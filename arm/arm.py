"""Arm module, used to train arm algorithm
"""
import copy

import numpy as np
import torch

try:
    import tensorflow as tf
    TENSORFLOW = True
except ImportError:
    TENSORFLOW = False


class Arm(torch.nn.Module):
    """Arm algorithm - initialized with arbitrary network
    that maps observations to vector of size one greater than
    the size of the action space. Can be trained by passing
    a replay buffer to the `train_batch` function
    function.

    Arguments:
        network {torch.nn.Module} -- arbitrary pytorch network
        iters {int} -- number of training iterations per batch
        mini_batch_size {int} -- number of samples per iteration
        tau {float} -- target network update offset
    """

    def __init__(self, network, iters, mini_batch_size, tau):
        super(Arm, self).__init__()

        self.network = network
        self.target_network = copy.deepcopy(network)
        self.iters = iters
        self.mini_batch_size = mini_batch_size
        self.tau = tau
        self.device = network.device

        self.epochs = 0
        self.steps = 0

    def __compute_targets(self, replay_buffer):

        first_batch = self.epochs == 0
        first_batch = False

        # precompute all v and q target values
        if first_batch:
            q_plus = torch.zeros([replay_buffer.curriculum_idcs.shape[0], 1])
        else:
            evs = torch.tensor([])
            cfvs = torch.tensor([])
            # compute q and v values of last iteration
            for obs, _, actions, *_ in replay_buffer.iterate(batch_size=512, curriculum=True):
                obs = torch.from_numpy(obs).to(self.device)
                actions = torch.from_numpy(
                    actions).unsqueeze(1).to(self.device)
                with torch.no_grad():
                    b_values = self.network(obs)
                    b_evs = b_values[:, :1]
                    b_cfvs = b_values[:, 1:]
                b_cfvs = torch.gather(b_cfvs, 1, actions)
                b_evs, b_cfvs = b_evs.cpu(), b_cfvs.cpu()
                evs = torch.cat((evs, b_evs.cpu()))
                cfvs = torch.cat((cfvs, b_cfvs.cpu()))
            # compute advantage value and clip to 0
            q_plus = torch.clamp(cfvs - evs, min=0)

        n_step = torch.from_numpy(
            replay_buffer.n_step[replay_buffer.curriculum_idcs]).unsqueeze(1)

        # set value target to n step rewards
        v_tar = n_step
        # add n step rewards on top of advantage values (cumulative advantage values)
        q_tar = q_plus + n_step

        return v_tar, q_tar

    def __sample_mini_batch(self, replay_buffer, v_tar, q_tar):

        # sample random batch from replay buffer indices
        mb_idcs = np.random.choice(
            replay_buffer.curriculum_idcs.shape[0], self.mini_batch_size)
        mb_obs, _, mb_actions, * \
            _ = replay_buffer[replay_buffer.curriculum_idcs[mb_idcs]]

        # initialize value estimate
        val_est_mb = torch.zeros(
            (self.mini_batch_size, 1)).to(self.device)

        # compute value estimate for non terminal nodes
        mb_est_rew_w = torch.from_numpy(
            replay_buffer.est_rew_weights[replay_buffer.curriculum_idcs[mb_idcs]])
        mb_est_non_zero = mb_est_rew_w.nonzero().squeeze()
        if mb_est_non_zero.numel():
            mb_est_rew_idcs = (replay_buffer.curriculum_idcs[mb_idcs] +
                               replay_buffer.n_step_size).reshape(-1)
            mb_v_prime_obs, _, mb_v_prime_actions, * \
                _ = replay_buffer[mb_est_rew_idcs]
            mb_v_prime_obs = mb_v_prime_obs
            mb_v_prime_actions = mb_v_prime_actions.astype(np.int64)
            mb_v_prime_obs = torch.from_numpy(
                mb_v_prime_obs).to(self.device)
            mb_v_prime_actions = torch.from_numpy(
                mb_v_prime_actions).to(self.device)

            with torch.no_grad():
                val_est = self.target_network(mb_v_prime_obs)[:, :1]
            val_est = val_est * replay_buffer.gamma**replay_buffer.n_step_size
            val_est_mb.index_add_(0, mb_est_non_zero.to(self.device), val_est)

        mb_obs = torch.from_numpy(mb_obs).to(self.device)
        mb_actions = torch.from_numpy(mb_actions).to(
            self.device).unsqueeze(1)

        # compute current v and q values
        mb_values = self.network(mb_obs)
        mb_v = mb_values[:, :1]
        mb_q = mb_values[:, 1:]
        action_space = mb_q.shape[-1]
        mb_q = torch.gather(mb_q, dim=1, index=mb_actions)
        # add value estimate onto target values
        mb_v_tar = v_tar[mb_idcs].to(self.device) + val_est_mb
        mb_q_tar = q_tar[mb_idcs].to(self.device) + val_est_mb
        return mb_v, mb_v_tar, mb_q, mb_q_tar, action_space

    def __reset_v_tar(self):
        self.target_network.load_state_dict(self.network.state_dict())

    def __update_v_target(self):
        target_params = self.target_network.parameters()
        params = self.network.parameters()

        for target, net in zip(target_params, params):
            target.data.add_(self.tau * (net.data - target.data))

    def __update_network(self, mb_v, mb_v_tar, mb_q, mb_q_tar, action_space=1):
        v_weight = 1 / (action_space + 1)
        q_weight = action_space / (action_space + 1)

        # compute loss and weight by action space
        self.network.optimizer.zero_grad()
        v_loss = self.network.criterion(mb_v, mb_v_tar)
        v_loss *= v_weight
        q_loss = self.network.criterion(mb_q, mb_q_tar)
        q_loss *= q_weight
        loss = v_loss + q_loss
        loss.backward()
        self.network.optimizer.step()

        v_loss = v_loss.detach().cpu()
        q_loss = q_loss.detach().cpu()

        return v_loss, q_loss

    def train_batch(self, replay_buffer, writer=None):
        """Trains the network with samples from the replay buffer
        using the arm algorithm. If a writer is passed, losses are
        recorded.

        Arguments:
            replay_buffer {ReplayBuffer} -- replay buffer of samples

        Keyword Arguments:
            writer {tf.summary.SummaryWriter} -- optional tensorflow
                                                 summary writer
                                                 (default: {None})
        """

        self.steps += len(replay_buffer)

        # precompute all target values
        print('computing target values...')
        v_tar, q_tar = self.__compute_targets(replay_buffer)

        # initialize cumulative loss buffers
        cum_v_loss = torch.tensor(0.0)
        cum_q_loss = torch.tensor(0.0)

        # reset value target network
        self.__reset_v_tar()

        print('training network...')

        for batch in range(self.iters):

            # sample mini batch
            mb_v, mb_v_tar, mb_q, mb_q_tar, action_space = self.__sample_mini_batch(
                replay_buffer, v_tar, q_tar)

            # update network
            v_loss, q_loss = self.__update_network(
                mb_v, mb_v_tar, mb_q, mb_q_tar, action_space)

            # update target network
            self.__update_v_target()

            # accumulate loss
            cum_v_loss += v_loss
            cum_q_loss += q_loss

            if writer is not None and TENSORFLOW:
                # write loss to summary writer
                with writer.as_default():
                    tf.summary.scalar(
                        'v_loss', v_loss.item(), self.epochs * self.iters + batch)
                    tf.summary.scalar(
                        'q_loss', q_loss.item(), self.epochs * self.iters + batch)

            if (batch + 1) % int(self.iters / 10) == 0:
                # print loss to console
                mean_v_loss = (cum_v_loss/int(self.iters / 10)).numpy()
                mean_q_loss = (cum_q_loss/int(self.iters / 10)).numpy()
                print('batch: {}, v_loss: {}, q_loss: {}'.format(
                    batch + 1, mean_v_loss, mean_q_loss))
                cum_v_loss.zero_()
                cum_q_loss.zero_()

        self.epochs += 1
