import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models.resnet as resnet
from torchvision.models.mobilenetv3 import InvertedResidualConfig, InvertedResidual

# Assume 3-dim tensor input N,C,L
class DenseAndPartialGPool(nn.Module):
	def __init__(self, input_length, output_length, nb_groups=8, nb_items_in_groups=8, channels_for_batchnorm=0):
		super().__init__()
		self.nb_groups = nb_groups
		self.nb_items_in_groups = nb_items_in_groups
		self.dense_input = input_length - nb_groups*nb_items_in_groups
		self.dense_output = output_length - 2*nb_groups
		self.dense_part = nn.Sequential(
			nn.Linear(self.dense_input, self.dense_output),
			nn.BatchNorm1d(channels_for_batchnorm) if channels_for_batchnorm > 0 else nn.Identity()
		)
		self.maxpool = nn.MaxPool1d(nb_items_in_groups)
		self.avgpool = nn.AvgPool1d(nb_items_in_groups)

	def forward(self, x):
		groups_for_gpool = x.split([self.nb_items_in_groups] * self.nb_groups + [self.dense_input], -1)
		maxpool_results = [ self.maxpool(y) for y in groups_for_gpool[:-1] ]
		avgpool_results = [ self.avgpool(y) for y in groups_for_gpool[:-1] ]
		
		dense_result = F.relu(self.dense_part(groups_for_gpool[-1]))

		x = torch.cat(maxpool_results + avgpool_results + [dense_result], -1)
		return x

# Assume 4-dim tensor input N,C,H,W
#
# Input            Output
#  C1    --↘  ↗--> Conv2D(C1 .. C3)
#  C2    ---==---> Conv2D(C1 .. C3)			Regular Conv2D
#  C3    --↗  ↘--> Conv2D(C1 .. C3)
#  C4    --------> MaxPool2D(C4) 			2 maxplanar, using kernel 3 on each plan
#  C5    --------> MaxPool2D(C5)
#  C6    -↘
#  C7    --------> Max(C6,C7,C8)
#  C8    -↗                                 2 groups of maxchannels, with 3 channels each
#  C9    -↘
#  C10   --------> Max(C9,C10,C11)
#  C11   -↗
class Conv2dAndPartialMaxPool(nn.Module):
	def __init__(self, input_length, output_length, kernel_conv=3, nb_channel_maxplanar=4, kernel_maxplanar=3, nb_groups_maxchannel=4, kernel_maxchannel=4, batchnorm=True):
		super().__init__()
		self.params_maxpool_planar  = (nb_channel_maxplanar, kernel_maxplanar)
		self.params_maxpool_channel = (nb_groups_maxchannel, kernel_maxchannel)

		self.conv_input = input_length - nb_channel_maxplanar - nb_groups_maxchannel*kernel_maxchannel
		self.conv_output = output_length - nb_channel_maxplanar - nb_groups_maxchannel
		self.conv_part = nn.Sequential(
			nn.Conv2d(self.conv_input, self.conv_output, kernel_conv, padding=kernel_conv//2),
			nn.BatchNorm2d(self.conv_output) if batchnorm else nn.Identity()
		)
		self.maxplanar  = nn.MaxPool2d(kernel_maxplanar, stride=1, padding=kernel_maxplanar//2)
		self.maxchannel = nn.MaxPool2d((kernel_maxchannel,1))

	def forward(self, x):
		groups_for_gpool = x.split([1] * self.params_maxpool_planar[0] + [self.params_maxpool_channel[1]] * self.params_maxpool_channel[0] + [self.conv_input], 1)
		# Max over plan, can use MaxPool2d directly
		maxplanar_results  = [ self.maxplanar(y) for y in groups_for_gpool[:self.params_maxpool_planar[0]] ]
		# Max over channels, need to permute dimensions before using MaxPool2d
		maxchannel_results = [ self.maxchannel(y.permute(0, 2, 1, 3)).permute(0, 2, 1, 3) for y in groups_for_gpool[self.params_maxpool_planar[0]:-1] ]
		conv_result = F.relu(self.conv_part(groups_for_gpool[-1]))

		x = torch.cat(maxplanar_results + maxchannel_results + [conv_result], 1)
		return x

# Assume 3-dim tensor input N,C,L, return N,1,L tensor
class FlattenAndPartialGPool(nn.Module):
	def __init__(self, length_to_pool, nb_channels_to_pool):
		super().__init__()
		self.length_to_pool = length_to_pool
		self.nb_channels_to_pool = nb_channels_to_pool
		self.maxpool = nn.MaxPool1d(nb_channels_to_pool)
		self.avgpool = nn.AvgPool1d(nb_channels_to_pool)
		self.flatten = nn.Flatten()

	def forward(self, x):
		x_begin, x_end = x[:,:,:self.length_to_pool], x[:,:,self.length_to_pool:]
		x_begin_firstC, x_begin_lastC = x_begin[:,:self.nb_channels_to_pool,:], x_begin[:,self.nb_channels_to_pool:,:]
		# MaxPool1D only applies to last dimension, whereas we want to apply on C dimension here
		x_begin_firstC = x_begin_firstC.transpose(-1, -2)
		maxpool_result = self.maxpool(x_begin_firstC).transpose(-1, -2)
		avgpool_result = self.avgpool(x_begin_firstC).transpose(-1, -2)
		x = torch.cat([
			self.flatten(maxpool_result),
			self.flatten(avgpool_result),
			self.flatten(x_begin_lastC),
			self.flatten(x_end)
		], 1)
		return x.unsqueeze(1)

class CatInTheMiddle(nn.Module):
	def __init__(self, layers2D, layers1D):
		super().__init__()
		self.layers2D, self.layers1D = layers2D, layers1D

	def forward(self, input2D, input1D):
		x = self.layers2D(input2D)
		x = torch.cat([torch.flatten(x, 1), torch.flatten(input1D, 1)], dim=-1)
		x = self.layers1D(x)
		return x


class SantoriniNNet(nn.Module):
	def __init__(self, game, args):
		# game params
		self.nb_vect, self.vect_dim = game.getBoardSize()
		self.action_size = game.getActionSize()
		self.num_players = 2
		self.args = args
		self.version = args['nn_version']

		super(SantoriniNNet, self).__init__()

		if self.version == -1:
			pass # Special case when loading empty NN from pit.py
		elif self.version == 1:
			self.dense1d_1 = nn.Sequential(
				nn.Linear(self.nb_vect*self.vect_dim, 128), nn.ReLU(),
			)
			self.partialgpool_1 = DenseAndPartialGPool(128, 128, nb_groups=6, nb_items_in_groups=3, channels_for_batchnorm=1)
			
			self.dense1d_2 = nn.Sequential(
				nn.Linear(128, 128), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(128, 128)                   , nn.ReLU(), # no batchnorm before max pooling
			)
			self.partialgpool_2 = DenseAndPartialGPool(128, 128, nb_groups=6, nb_items_in_groups=3, channels_for_batchnorm=1)

			self.dense1d_3 = nn.Sequential(
				nn.Linear(128, 128), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(128, 128), nn.BatchNorm1d(1), nn.ReLU(),
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(128, 128),
				nn.Linear(128, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(128, 128),
				nn.Linear(128, self.num_players)
			)


		elif self.version == 10:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d( 2, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.partialgpool_1 = Conv2dAndPartialMaxPool(64, 64, kernel_conv=3, nb_channel_maxplanar=4, kernel_maxplanar=3, nb_groups_maxchannel=4, kernel_maxchannel=4)
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(64, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
			)

			self.dense1d_1 = nn.Sequential(
				nn.Linear(32*5*5, 256), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(256, 256), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(256, 256)                   , nn.ReLU(), # no batchnorm before max pooling
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(256, 128),
				nn.Linear(128, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(256, 128),
				nn.Linear(128, self.num_players)
			)

		elif self.version == 11:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d( 3, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.partialgpool_1 = nn.Identity()
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(64, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
			)

			self.dense1d_1 = nn.Sequential(
				nn.Linear(32*5*5, 256), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(256, 256), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(256, 256)                   , nn.ReLU(), # no batchnorm before max pooling
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(256, 128),
				nn.Linear(128, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(256, 128),
				nn.Linear(128, self.num_players)
			)

		elif self.version == 12:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d( 2, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.partialgpool_1 = Conv2dAndPartialMaxPool(128, 128, kernel_conv=3, nb_channel_maxplanar=8, kernel_maxplanar=3, nb_groups_maxchannel=8, kernel_maxchannel=5)
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(128, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)

			self.dense1d_1 = nn.Sequential(
				nn.Linear(64*5*5, 256), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(256, 256), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(256, 256), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(256, 256)                   , nn.ReLU(), # no batchnorm before max pooling
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(256, 128),
				nn.Linear(128, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(256, 128),
				nn.Linear(128, self.num_players)
			)

		elif self.version == 13:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d( 2, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(32, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
				nn.Conv2d(32, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
				nn.Conv2d(32, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
			)
			self.partialgpool_1 = nn.Identity()
			self.conv2d_3 = nn.Identity()

			self.dense1d_1 = nn.Sequential(
				nn.Linear(32*5*5, 1024), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(1024, 512), nn.BatchNorm1d(1), nn.ReLU(),
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(512, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(512, self.num_players)
			)

		elif self.version == 20:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d( 2, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.partialgpool_1 = nn.Identity()
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.dense1d_0 = nn.Sequential(
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
			)


			self.dense1d_1 = nn.Sequential(
				nn.Linear((64+1)*5*5, 512), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(512, 512), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(512, 512)                   , nn.ReLU(), # no batchnorm before max pooling
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(512, 256),
				nn.Linear(256, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(512, 256),
				nn.Linear(256, self.num_players)
			)

		elif self.version == 21:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d(  2, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.partialgpool_1 = nn.Identity()
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.dense1d_0 = nn.Sequential(
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
			)


			self.dense1d_1 = nn.Sequential(
				nn.Linear((128+1)*5*5, 1024), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(1024, 1024), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(1024, 1024)                   , nn.ReLU(), # no batchnorm before max pooling
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(1024, 256),
				nn.Linear(256, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(1024, 256),
				nn.Linear(256, self.num_players)
			)

		elif self.version == 22:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d( 2, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.partialgpool_1 = nn.Identity()
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
				nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
			)
			self.dense1d_0 = nn.Sequential(
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
			)


			self.dense1d_1 = nn.Sequential(
				nn.Linear((64+1)*5*5, 512), nn.ReLU(),
				nn.Linear(       512, 512), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(512, 512), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(512, 512), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(512, 512), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(512, 512)                   , nn.ReLU(), # no batchnorm before max pooling
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(512, 512),
				nn.Linear(512, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(512, 512),
				nn.Linear(512, self.num_players)
			)

		elif self.version == 23:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d(  2, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.partialgpool_1 = nn.Identity()
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.dense1d_0 = nn.Sequential(
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
			)


			self.dense1d_1 = nn.Sequential(
				nn.Linear((128+1)*5*5, 512), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(512, 512), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(512, 256)                   , nn.ReLU(), # no batchnorm before max pooling
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(256, 256),
				nn.Linear(256, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(256, 256),
				nn.Linear(256, self.num_players)
			)

		elif self.version == 24:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d(  2, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1)                     , nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.partialgpool_1 = Conv2dAndPartialMaxPool(128, 128, kernel_conv=3, nb_channel_maxplanar=4, kernel_maxplanar=3, nb_groups_maxchannel=2, kernel_maxchannel=4)
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.dense1d_0 = nn.Sequential(
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
			)


			self.dense1d_1 = nn.Sequential(
				nn.Linear((128+1)*5*5, 512), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(512, 512), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(512, 256), nn.BatchNorm1d(1), nn.ReLU(),
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(256, 256),
				nn.Linear(256, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(256, 256),
				nn.Linear(256, self.num_players)
			)

		elif self.version == 25:
			self.conv2d_1 = nn.Sequential(
				nn.Conv2d(  2, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.conv2d_2 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.partialgpool_1 = Conv2dAndPartialMaxPool(128, 128, kernel_conv=3, nb_channel_maxplanar=4, kernel_maxplanar=3, nb_groups_maxchannel=2, kernel_maxchannel=4)
			self.conv2d_3 = nn.Sequential(
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
				nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
			)
			self.dense1d_0 = nn.Sequential(
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(5*5, 5*5), nn.BatchNorm1d(1), nn.ReLU(),
			)


			self.dense1d_1 = nn.Sequential(
				nn.Linear((128+1)*5*5, 512), nn.BatchNorm1d(1), nn.ReLU(),
			)
			self.dense1d_2 = nn.Sequential(
				nn.Linear(512, 512), nn.BatchNorm1d(1), nn.ReLU(),
				nn.Linear(512, 256), nn.BatchNorm1d(1), nn.ReLU(),
			)

			self.output_layers_PI = nn.Sequential(
				nn.Linear(256, 256),
				nn.Linear(256, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				nn.Linear(256, self.num_players)
			)

		elif self.version == 70:
			n_filters = 128
			self.first_layer = nn.Conv2d(  2, n_filters, 3, padding=1, bias=False)
			
			downsample_trunk = nn.Sequential(
				nn.Conv2d(n_filters, n_filters//2, 1, bias=False),
				nn.BatchNorm2d(n_filters//2)
			)
			self.trunk = nn.Sequential(
				resnet.Bottleneck(n_filters, n_filters//4, groups=32, base_width=8),
				resnet.Bottleneck(n_filters, n_filters//4, groups=32, base_width=8),
				resnet.Bottleneck(n_filters, n_filters//8, groups=32, base_width=8, downsample=downsample_trunk),
			)

			self.output_layers_PI = nn.Sequential(
				resnet.BasicBlock(n_filters//2, n_filters//2),
				nn.Flatten(1),
				nn.Linear(n_filters//2 *5*5, self.action_size),
				nn.ReLU(),
				nn.Linear(self.action_size, self.action_size)
			)

			self.output_layers_V = nn.Sequential(
				resnet.BasicBlock(n_filters//2, n_filters//2),
				nn.Flatten(1),
				nn.Linear(n_filters//2 *5*5, self.num_players),
				nn.ReLU(),
				nn.Linear(self.num_players, self.num_players)
			)

		elif self.version == 66:
			n_filters = 32
			n_filters_first = n_filters//2
			n_filters_begin = n_filters_first
			n_filters_end = n_filters
			n_exp_begin, n_exp_end = n_filters_begin*3, n_filters_end*3
			depth = 12 - 2

			def inverted_residual(input_ch, expanded_ch, out_ch, use_se, activation, kernel=3, stride=1, dilation=1, width_mult=1):
				return InvertedResidual(InvertedResidualConfig(input_ch, kernel, expanded_ch, out_ch, use_se, activation, stride, dilation, width_mult), nn.BatchNorm2d)
			self.first_layer = nn.Conv2d(  2, n_filters_first, 3, padding=1, bias=False)
			confs  = [inverted_residual(n_filters_first if i==0 else n_filters_begin, n_exp_begin, n_filters_begin, False, "RE") for i in range(depth//2)]
			confs += [inverted_residual(n_filters_begin if i==0 else n_filters_end  , n_exp_end  , n_filters_end  , True , "HS") for i in range(depth//2)]
			confs += [inverted_residual(n_filters_end                               , n_exp_end  , n_filters      , True , "HS")]
			self.trunk = nn.Sequential(*confs)

			head_depth = 6
			n_exp_head = n_filters * 3
			head_PI = [inverted_residual(n_filters, n_exp_head, n_filters, True, "HS",) for i in range(head_depth)] + [
				nn.Flatten(1),
				nn.Linear(n_filters *5*5, self.action_size),
				nn.ReLU(),
				nn.Linear(self.action_size, self.action_size)
			]
			self.output_layers_PI = nn.Sequential(*head_PI)

			head_V = [inverted_residual(n_filters, n_exp_head, n_filters, True, "HS",) for i in range(head_depth)] + [
				nn.Flatten(1),
				nn.Linear(n_filters *5*5, self.num_players),
				nn.ReLU(),
				nn.Linear(self.num_players, self.num_players)
			]
			self.output_layers_V = nn.Sequential(*head_V)

		elif self.version == 67:
			n_filters = 32
			n_filters_first = n_filters//2
			n_filters_begin = n_filters_first
			n_filters_end = n_filters
			n_exp_begin, n_exp_end = n_filters_begin*3, n_filters_end*3
			depth = 12 - 2

			def inverted_residual(input_ch, expanded_ch, out_ch, use_se, activation, kernel=3, stride=1, dilation=1, width_mult=1):
				return InvertedResidual(InvertedResidualConfig(input_ch, kernel, expanded_ch, out_ch, use_se, activation, stride, dilation, width_mult), nn.BatchNorm2d)
			self.first_layer = nn.Conv2d(  2, n_filters_first, 3, padding=1, bias=False)
			confs  = [inverted_residual(n_filters_first if i==0 else n_filters_begin, n_exp_begin, n_filters_begin, False, "RE") for i in range(depth//2)]
			confs += [inverted_residual(n_filters_begin if i==0 else n_filters_end  , n_exp_end  , n_filters_end  , True , "HS") for i in range(depth//2)]
			confs += [inverted_residual(n_filters_end                               , n_exp_end  , n_filters      , True , "HS")]
			self.trunk = nn.Sequential(*confs)

			head_depth = 6
			n_exp_head = n_filters * 3
			head_PI_2D = [inverted_residual(n_filters, n_exp_head, n_filters, True, "HS",) for i in range(head_depth)]
			head_PI_1D = [
				nn.Linear((n_filters+1) *5*5, self.action_size),
				nn.ReLU(),
				nn.Linear(self.action_size, self.action_size)
			]
			self.output_layers_PI = CatInTheMiddle(nn.Sequential(*head_PI_2D), nn.Sequential(*head_PI_1D))

			head_V_2D = [inverted_residual(n_filters, n_exp_head, n_filters, True, "HS",) for i in range(head_depth)]
			head_V_1D = [
				nn.Linear((n_filters+1) *5*5, self.num_players),
				nn.ReLU(),
				nn.Linear(self.num_players, self.num_players)
			]
			self.output_layers_V = CatInTheMiddle(nn.Sequential(*head_V_2D), nn.Sequential(*head_V_1D))

		else:
			raise Exception(f'Warning, unknown NN version {self.version}')

		self.register_buffer('lowvalue', torch.FloatTensor([-1e8]))
		def _init(m):
			if type(m) == nn.Linear:
				nn.init.kaiming_uniform_(m.weight)
				nn.init.zeros_(m.bias)
			elif type(m) == nn.Sequential:
				for module in m:
					_init(module)
		for _, layer in self.__dict__.items():
			if isinstance(layer, nn.Module):
				layer.apply(_init)

	def forward(self, input_data, valid_actions):
		if self.version == 1:
			x = input_data.transpose(-1, -2).view(-1, self.vect_dim, self.nb_vect)
		
			x = torch.flatten(x, start_dim=1).unsqueeze(1)
			x = F.dropout(self.dense1d_1(x)     , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.partialgpool_1(x), p=self.args['dropout'], training=self.training)
			x = F.dropout(self.dense1d_2(x)     , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.partialgpool_2(x), p=self.args['dropout'], training=self.training)
			x = F.dropout(self.dense1d_3(x)     , p=self.args['dropout'], training=self.training)
			
			v = self.output_layers_V(x).squeeze(1)
			pi = torch.where(valid_actions, self.output_layers_PI(x).squeeze(1), self.lowvalue)

		elif self.version in [10, 11, 12, 13]:
			x = input_data.transpose(-1, -2).view(-1, 3, 5, 5)
			
			x = F.dropout(self.conv2d_1(x)      , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.conv2d_2(x)      , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.partialgpool_1(x), p=self.args['dropout'], training=self.training)
			x = F.dropout(self.conv2d_3(x)      , p=self.args['dropout'], training=self.training)

			x = torch.flatten(x, start_dim=1).unsqueeze(1)
			x = F.dropout(self.dense1d_1(x)     , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.dense1d_2(x)     , p=self.args['dropout'], training=self.training)
			
			v = self.output_layers_V(x).squeeze(1)
			pi = torch.where(valid_actions, self.output_layers_PI(x).squeeze(1), self.lowvalue)

		elif self.version in [20, 21, 22, 23, 24, 25]:
			x = input_data.transpose(-1, -2).view(-1, 3, 5, 5)
			x, data = x.split([2,1], dim=1)

			x = F.dropout(self.conv2d_1(x)      , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.conv2d_2(x)      , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.partialgpool_1(x), p=self.args['dropout'], training=self.training)
			x = F.dropout(self.conv2d_3(x)      , p=self.args['dropout'], training=self.training)

			data = torch.flatten(data, start_dim=2)
			data = F.dropout(self.dense1d_0(data), p=self.args['dropout'], training=self.training)

			x = torch.flatten(x, start_dim=1).unsqueeze(1)
			x = torch.cat([x, data], dim=-1)
			x = F.dropout(self.dense1d_1(x)     , p=self.args['dropout'], training=self.training)
			x = F.dropout(self.dense1d_2(x)     , p=self.args['dropout'], training=self.training)
			
			v = self.output_layers_V(x).squeeze(1)
			pi = torch.where(valid_actions, self.output_layers_PI(x).squeeze(1), self.lowvalue)

		elif self.version in [66, 70]:
			x = input_data.transpose(-1, -2).view(-1, 3, 5, 5)
			x, data = x.split([2,1], dim=1)
			x = self.first_layer(x)
			x = self.trunk(x)
			v = self.output_layers_V(x)
			pi = torch.where(valid_actions, self.output_layers_PI(x), self.lowvalue)

		elif self.version in [67]:
			x = input_data.transpose(-1, -2).view(-1, 3, 5, 5)
			x, data = x.split([2,1], dim=1)
			x = self.first_layer(x)
			x = self.trunk(x)
			v = self.output_layers_V(x, data)
			pi = torch.where(valid_actions, self.output_layers_PI(x, data), self.lowvalue)

		return F.log_softmax(pi, dim=1), torch.tanh(v)
