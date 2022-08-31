import argparse
import json
import os
from tqdm import tqdm

import numpy as np
import torch
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import torch.nn.functional as F
torch.backends.cudnn.benchmark = True

from model import AIM
from data import CARLA_Data
from config import GlobalConfig


parser = argparse.ArgumentParser()
parser.add_argument('--id', type=str, default='aim', help='Unique experiment identifier.')
parser.add_argument('--device', type=str, default='cuda', help='Device to use')
parser.add_argument('--epochs', type=int, default=101, help='Number of train epochs.')
parser.add_argument('--lr', type=float, default=1e-4, help='Learning rate.')
parser.add_argument('--val_every', type=int, default=3, help='Validation frequency (epochs).')
parser.add_argument('--batch_size', type=int, default=24, help='Batch size')
parser.add_argument('--logdir', type=str, default='log', help='Directory to log data to.')

parser.add_argument('--sst', type=int, default=0, help='Self-supervised training')
parser.add_argument('--load_model', type=int, default=0, help='Load model')
parser.add_argument('--ssd_dir', type=str, help='SSD Data Folder')
parser.add_argument('--framework', type=str, help='Which framework')



args = parser.parse_args()
args.logdir = os.path.join(args.logdir, args.id)

writer = SummaryWriter(log_dir=args.logdir)


class Engine(object):
	"""Engine that runs training and inference.
	Args
		- cur_epoch (int): Current epoch.
		- print_every (int): How frequently (# batches) to print loss.
		- validate_every (int): How frequently (# epochs) to run validation.
		
	"""

	def __init__(self, config, model, optimizer, val_dataloader, ss_dataloader, cur_epoch=0, cur_iter=0):
		self.cur_epoch = cur_epoch
		self.cur_iter = cur_iter
		self.bestval_epoch = cur_epoch
		self.train_loss = []
		self.val_loss = []
		self.bestval = 1e10

		self.config = config
		self.model = model
		self.optimizer = optimizer
		self.train_dataloader = None
		self.val_dataloader = val_dataloader
		self.ss_dataloader = ss_dataloader
		

	def train(self):
		loss_epoch = 0.
		num_batches = 0
		self.model.train()

		# Train loop
		for data in tqdm(self.train_dataloader):
			
			# efficiently zero gradients
			for p in self.model.parameters():
				p.grad = None
			
			# create batch and move to GPU
			fronts_in = data['fronts']
			fronts = []
			for i in range(self.config.seq_len):
				fronts.append(fronts_in[i].to(args.device, dtype=torch.float32))

			# target point
			# command = data['command'].to(args.device)
			# gt_velocity = data['velocity'].to(args.device, dtype=torch.float32)
			target_point = torch.stack(data['target_point'], dim=1).to(args.device, dtype=torch.float32)

			# inference
			encoding = [self.model.image_encoder(fronts)]

			pred_wp = self.model(encoding, target_point)
			
			gt_waypoints = [torch.stack(data['waypoints'][i], dim=1).to(args.device, dtype=torch.float32) for i in range(self.config.seq_len, len(data['waypoints']))]
			gt_waypoints = torch.stack(gt_waypoints, dim=1).to(args.device, dtype=torch.float32)
			loss = F.l1_loss(pred_wp, gt_waypoints, reduction='none').mean()
			loss.backward()
			loss_epoch += float(loss.item())

			num_batches += 1
			self.optimizer.step()

			writer.add_scalar('train_loss', loss.item(), self.cur_iter)
			self.cur_iter += 1
		
		
		loss_epoch = loss_epoch / num_batches
		self.train_loss.append(loss_epoch)
		self.cur_epoch += 1

	def get_labels(self):
		self.model.eval()
		os.system(f"mkdir -p {self.config.local_root_dir}/../ssd_data/{args.ssd_dir}/")
		preload_file = os.path.join(self.config.local_root_dir+f'/../ssd_data/{args.ssd_dir}/', 'rg_aim_pl_'+str(self.config.seq_len)+'_'+str(self.config.pred_len)+'.npy')

		preload_front = []
		preload_x = []
		preload_y = []
		preload_theta = []
		preload_x_command = []
		preload_y_command = []
		preload_waypoints = []

		with torch.no_grad():	

			# Validation loop
			for data in tqdm(self.ss_dataloader):
				
				# create batch and move to GPU
				fronts_in = data['fronts']
				fronts = []
				for i in range(self.config.seq_len):
					fronts.append(fronts_in[i].to(args.device, dtype=torch.float32))

				# driving labels
				# command = data['command'].to(args.device)
				# gt_velocity = data['velocity'].to(args.device, dtype=torch.float32)

				# target point
				target_point = torch.stack(data['target_point'], dim=1).to(args.device, dtype=torch.float32)

				# inference
				encoding = [self.model.image_encoder(fronts)]

				pred_wp = self.model(encoding, target_point)

				for i in range(len(pred_wp)):
					preload_front.append([data['ssd_fronts'][0][i]])
					preload_x.append([0]*(self.config.pred_len+1))
					preload_y.append([0]*(self.config.pred_len+1))
					preload_theta.append([0]*(self.config.pred_len+1))
					preload_x_command.append(data['x_command'][i].item())
					preload_y_command.append(data['y_command'][i].item())
					wp = [(0.0,0.0)]
					for j in range(len(pred_wp[0])):
						wp.append((pred_wp[i][j][0].item(), pred_wp[i][j][1].item()))
					preload_waypoints.append(wp)
					assert len(preload_x) == len(preload_waypoints)

		preload_dict = {}
		preload_dict['front'] = preload_front
		preload_dict['x'] = preload_x
		preload_dict['y'] = preload_y
		preload_dict['theta'] = preload_theta
		preload_dict['x_command'] = preload_x_command
		preload_dict['y_command'] = preload_y_command
		preload_dict['waypoints'] = preload_waypoints
		np.save(preload_file, preload_dict)


	def validate(self):
		self.model.eval()

		with torch.no_grad():	
			num_batches = 0
			wp_epoch = 0.

			# Validation loop
			for batch_num, data in enumerate(tqdm(self.val_dataloader), 0):
				
				# create batch and move to GPU
				fronts_in = data['fronts']
				fronts = []
				for i in range(self.config.seq_len):
					fronts.append(fronts_in[i].to(args.device, dtype=torch.float32))

				# driving labels
				# command = data['command'].to(args.device)
				# gt_velocity = data['velocity'].to(args.device, dtype=torch.float32)

				# target point
				target_point = torch.stack(data['target_point'], dim=1).to(args.device, dtype=torch.float32)

				# inference
				encoding = [self.model.image_encoder(fronts)]

				pred_wp = self.model(encoding, target_point)

				gt_waypoints = [torch.stack(data['waypoints'][i], dim=1).to(args.device, dtype=torch.float32) for i in range(self.config.seq_len, len(data['waypoints']))]
				gt_waypoints = torch.stack(gt_waypoints, dim=1).to(args.device, dtype=torch.float32)
				wp_epoch += float(F.l1_loss(pred_wp, gt_waypoints, reduction='none').mean())

				num_batches += 1
					
			wp_loss = wp_epoch / float(num_batches)
			tqdm.write(f'Epoch {self.cur_epoch:03d}, Batch {batch_num:03d}:' + f' Wp: {wp_loss:3.3f}')

			writer.add_scalar('val_loss', wp_loss, self.cur_epoch)
			self.val_loss.append(wp_loss)

	def save(self):

		save_best = False
		if self.val_loss[-1] <= self.bestval:
			self.bestval = self.val_loss[-1]
			self.bestval_epoch = self.cur_epoch
			save_best = True
		
		# Create a dictionary of all data to save
		log_table = {
			'epoch': self.cur_epoch,
			'iter': self.cur_iter,
			'bestval': self.bestval,
			'bestval_epoch': self.bestval_epoch,
			'train_loss': self.train_loss,
			'val_loss': self.val_loss,
		}

		# Save the recent model/optimizer states
		torch.save(self.model.state_dict(), os.path.join(args.logdir, 'model.pth'))
		torch.save(self.optimizer.state_dict(), os.path.join(args.logdir, 'recent_optim.pth'))

		# Log other data corresponding to the recent model
		with open(os.path.join(args.logdir, 'recent.log'), 'w') as f:
			f.write(json.dumps(log_table))

		tqdm.write('====== Saved recent model ======>')
		
		if save_best:
			torch.save(self.model.state_dict(), os.path.join(args.logdir, 'best_model.pth'))
			torch.save(self.optimizer.state_dict(), os.path.join(args.logdir, 'best_optim.pth'))
			tqdm.write('====== Overwrote best model ======>')


with open(f'{args.framework}.py', 'r') as f:
	exec(f.read())


# Config
# config = GlobalConfig(train_with_ssd_data=args.sst, ssd_data=args.ssd_data)

# # # Data
# # train_set = CARLA_Data(root=config.train_data, config=config)
# ssd_set = CARLA_Data(root=config.ssd_data, config=config, is_imgaug=False)
# val_set = CARLA_Data(root=config.val_data, config=config, is_imgaug=False)

# # dataloader_train = DataLoader(train_set, batch_size=args.batch_size, shuffle=True, num_workers=8, pin_memory=True)
# self.ss_dataloader = DataLoader(ssd_set, batch_size=args.batch_size, shuffle=True, num_workers=8, pin_memory=True)
# dataloader_val = DataLoader(val_set, batch_size=args.batch_size, shuffle=False, num_workers=8, pin_memory=True)

# # Model
# model = AIM(config, args.device)
# optimizer = optim.AdamW(model.parameters(), lr=args.lr)
# trainer = Engine()

# model_parameters = filter(lambda p: p.requires_grad, model.parameters())
# params = sum([np.prod(p.size()) for p in model_parameters])
# print ('Total trainable parameters: ', params)

# # Create logdir
# # if not os.path.isdir(args.logdir):
# # 	os.makedirs(args.logdir)
# # 	print('Created dir:', args.logdir)
# # elif os.path.isfile(os.path.join(args.logdir, 'recent.log')):
# # 	print('Loading checkpoint from ' + args.logdir)
# # 	with open(os.path.join(args.logdir, 'recent.log'), 'r') as f:
# # 		log_table = json.load(f)

# # 	# Load variables
# # 	trainer.cur_epoch = log_table['epoch']
# # 	if 'iter' in log_table: trainer.cur_iter = log_table['iter']
# # 	trainer.bestval = log_table['bestval']
# # 	trainer.train_loss = log_table['train_loss']
# # 	trainer.val_loss = log_table['val_loss']

# # 	# Load checkpoint

# # model.load_state_dict(torch.load(os.path.join(args.logdir, 'model.pth')))
# # 	optimizer.load_state_dict(torch.load(os.path.join(args.logdir, 'recent_optim.pth')))

# # Log args
# with open(os.path.join(args.logdir, 'args.txt'), 'w') as f:
# 	json.dump(args.__dict__, f, indent=2)

# # for epoch in range(trainer.cur_epoch, args.epochs): 
# # 	trainer.train()
# # 	if epoch % args.val_every == 0: 
# # 		trainer.validate()
# # 		trainer.save()

# if config.train_with_ssd_data:
# 	print("Training with Pseudolabels")
# 	train_set = CARLA_Data(root=(config.ssd_train_data+config.train_data), config=config, is_imgaug=True)
# 	dataloader_train = DataLoader(train_set, batch_size=args.batch_size, shuffle=True, num_workers=8, pin_memory=True)
# 	for epoch in range(trainer.cur_epoch, args.epochs): 
# 		trainer.train()
# 		if epoch % args.val_every == 0: 
# 			trainer.validate()
# 			trainer.save()
# 	print("Collect Labels")
# 	trainer.get_labels()
# 	# print("Fine Tuning")
# 	# train_set = CARLA_Data(root=config.train_data, config=config)
# 	# dataloader_train = DataLoader(train_set, batch_size=args.batch_size, shuffle=True, num_workers=8, pin_memory=True)
# 	# for epoch in range(trainer.cur_epoch, args.epochs): 
# 	# 	trainer.train()
# 	# 	if epoch % args.val_every == 0: 
# 	# 		trainer.validate()
# 	# 		trainer.save()

# if not config.train_with_ssd_data:
# 	print("Supervised Training")
# 	train_set = CARLA_Data(root=config.train_data, config=config, is_imgaug=True)
# 	dataloader_train = DataLoader(train_set, batch_size=args.batch_size, shuffle=True, num_workers=8, pin_memory=True)
# 	for epoch in range(trainer.cur_epoch, args.epochs): 
# 		trainer.train()
# 		if epoch % args.val_every == 0: 
# 			trainer.validate()
# 			trainer.save()
# 	print("Collect Labels")
# 	trainer.get_labels()