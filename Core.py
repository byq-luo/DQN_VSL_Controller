# -*- coding: utf-8 -*-
"""
Created on Sun Jan  6 13:36:09 2019

@author: ChocolateDave
"""

# Import Modules
import collections
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import os,sys
sys.path.append("./lib")
sys.path.append("./common")

import Env as Env
from torch.autograd import Variable
from tensorboardX import SummaryWriter
from common import action, agent, utils, experience, tracker, wrapper

# Global Variable:
#parser.add_argument("--resume", default = None, type = str, metavar= path, help= 'path to latest checkpoint')
params = utils.Constants

# Build Up Neural Network
class DuelingNetwork(nn.Module):
    """
    Create a neural network to convert image data
    """
    def __init__(self, input_shape, n_actions):
        super(DuelingNetwork, self).__init__()

        self.Convolutional_Layer = nn.Sequential(
            nn.Conv2d(input_shape[0], 32, kernel_size= 9, stride= 4),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size= 5, stride= 2),
            nn.ReLU(),
            nn.Conv2d(64, 256, kernel_size= 3, stride= 1),
            nn.ReLU()
        )

        #print('input_shape[0]: ', input_shape[0])
        conv_out_size = self._get_conv_out(input_shape)
        self.Fully_Connected_adv = nn.Sequential(
            nn.Linear(conv_out_size, 512),
            nn.ReLU(),
            nn.Linear(512, n_actions)
        )
        self.Fully_Connected_val = nn.Sequential(
            nn.Linear(conv_out_size, 512),
            nn.ReLU(),
            nn.Linear(512, 1)
        )

    def _get_conv_out(self, shape):
        #print('1, *shape: ', torch.zeros(1, *shape).size())
        o = self.Convolutional_Layer(torch.zeros(1, *shape))
        return int(np.prod(o.size()))

    def forward(self, x):
        fx = x.float()
        #print(x.size())
        conv_out = self.Convolutional_Layer(fx).view(fx.size()[0], -1)
        val = self.Fully_Connected_val(conv_out)
        adv = self.Fully_Connected_adv(conv_out)
        return val + adv - adv.mean()

# Saving model
def save_model(net, optim, path, frame):
	state_dict = net.state_dict()
	for key in state_dict.keys():
		state_dict[key] = state_dict[key].cpu()

	torch.save({
		'frame': frame,
		'state_dict': state_dict,
		'optimizer': optim},
		path)

# Load pretrained model
def load_model(net, path):
	state_dict = torch.load(path)
	new_state_dict = collections.OrderedDict()
	for k, value in state_dict['state_dict'].iteritems():
		key = "module.{}".format(k)
		new_state_dict[key] = value
	net.load_state_dict(new_state_dict)
	frame = state_dict['frame']
	print("Having pre-trained %d frames." % frame)
	optimizer = state_dict['optimizer']
	net.train()
	return net, frame, optimizer

# Training
def Core():   
    writer = SummaryWriter(comment = '-VSL-Dueling')
    env = Env.SumoEnv()  ###This IO needs to be modified
    #env = env.unwrapped
    #print(env_traino.state_shape)
    env = wrapper.wrap_dqn(env, stack_frames = 3, episodic_life= False, reward_clipping= False)  ###wrapper could be modified
    #print(env.observation_space.shape)
    net = DuelingNetwork(env.observation_space.shape, env.action_space.n)
    
    path = os.path.join('./runs/', 'checkpoint.pth')
    print("CUDA™ is " + ("AVAILABLE" if torch.cuda.is_available() else "NOT AVAILABLE"))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if torch.cuda.is_available():
        c = input("Please assign a gpu core (int, <" + str(torch.cuda.device_count()) + "): ")
        gpu = int(c) if c is not '' else 0
        if gpu is not None:
            torch.cuda.set_device(gpu)
            torch.set_default_tensor_type('torch.cuda.FloatTensor')
            net.cuda()
            torch.backends.cudnn.benchmark = True
        print("Now using GPU CORE #{} for training".format(torch.cuda.current_device()))

    tgt_net = agent.TargetNet(net)
    selector = action.EpsilonGreedyActionSelector(epsilon=params['epsilon_start'])
    epsilon_tracker = tracker.EpsilonTracker(selector, params)
    agents = agent.DQNAgent(net, selector, writer, device = device)

    exp_source = experience.ExperienceSourceFirstLast(env, agents, gamma=params['gamma'], steps_count=1)
    #buffer = experience.ExperienceReplayBuffer(exp_source, params['replay_size']) # For Regular memory optimization
    buffer = experience.PrioReplayBuffer(exp_source, params['replay_size'],params['PRIO_REPLAY_ALPHA']) #For Prioritized memory optimization

    frame_idx = 0
    beta = params['BETA_START']

    if path:
        if os.path.isfile(path):
            print("=> Loading checkpoint '{}'".format(path))
            net, frame_idx, optimizer = load_model(net, path)
            print("Checkpoint loaded successfully! ")
        else:
            optimizer = optim.Adam(net.parameters(), lr=params['learning_rate'])
            print("=> No such checkpoint at '{}'".format(path))

    with tracker.RewardTracker(writer, params['stop_reward'], params['stop_frame']) as reward_tracker:  #stop reward needs to be modified according to reward function
        while True:
            frame_idx += 1
            buffer.populate(1)
            epsilon_tracker.frame(frame_idx)
            beta = min(1.0, params['BETA_START'] + frame_idx * (1.0 - params['BETA_START']) / params['BETA_FRAMES'])
            writer.add_scalar("Train/Beta", beta, frame_idx)

            new_rewards, env_status = exp_source.pop_total_rewards()
            if new_rewards:
                #writer.add_scalar("beta", beta, frame_idx)
                for name, netparam in net.named_parameters():
                    writer.add_histogram('Model/' + name, netparam.clone().cpu().data.numpy(), frame_idx)
                for key in env_status.keys():
                    writer.add_scalar('Env/' + str(key), env_status[key], frame_idx)
                if reward_tracker.reward(new_rewards[0], frame_idx, selector.epsilon):
                    env.close()
                    break

            if len(buffer) < params['replay_initial']:
                continue

            #Regular memory optimization
            '''optimizer.zero_grad()
            batch = buffer.sample(params['batch_size'])
            loss_v = utils.calc_loss_dqn(batch, net, tgt_net.target_model, gamma=params['gamma'], device=device)
            loss_v.backward()
            optimizer.step()'''

            #Prioritized memory optimization
            optimizer.zero_grad()
            batch, batch_indices, batch_weights = buffer.sample(params['batch_size'], beta)
            loss_v, sample_prios_v = utils.calc_loss(batch, batch_weights, net, tgt_net.target_model,
                                               params['gamma'], device=device)
            loss_v.backward()
            optimizer.step()
            buffer.update_priorities(batch_indices, sample_prios_v.data.cpu().numpy())

            #Writer function -> Tensorboard file
            writer.add_scalar("Train/Loss", loss_v, frame_idx)
            
            #saving model
            if frame_idx % 1000== 0:
                save_model(net, optimizer, path, frame_idx)
                print("Network saved at %s" % path)
            
            if frame_idx % params['max_tau'] == 0:
                tgt_net.sync()  #Sync q_eval and q_target

if __name__ == '__main__':
    Core()