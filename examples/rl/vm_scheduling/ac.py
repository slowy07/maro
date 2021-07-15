# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import os
import sys

import numpy as np
import scipy
import torch

from maro.rl.experience import ExperienceSet, ExperienceStore, UniformSampler
from maro.rl.model import DiscreteACNet, FullyConnectedBlock, OptimOption
from maro.rl.policy.algorithms import ActorCritic, ActorCriticConfig

vm_path = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, vm_path)
from env_wrapper import STATE_DIM

config = {
    "model": {
        "network": {
            "actor": {
                "input_dim": STATE_DIM,
                "output_dim": 9,
                "hidden_dims": [64, 32, 32],
                "activation": "leaky_relu",
                "softmax": True,
                "batch_norm": False,
                "head": True
            },
            "critic": {
                "input_dim": STATE_DIM,
                "output_dim": 1,
                "hidden_dims": [256, 128, 64],
                "activation": "leaky_relu",
                "softmax": False,
                "batch_norm": False,
                "head": True
            }
        },
        "optimization": {
            "actor": {
                "optim_cls": "adam",
                "optim_params": {"lr": 0.0001}
            },
            "critic": {
                "optim_cls": "sgd",
                "optim_params": {"lr": 0.001}
            }
        }
    },
    "algorithm": {
        "reward_discount": 0.9,
        "train_epochs": 100,
        "gradient_iters": 1,
        "critic_loss_cls": "mse",
        "actor_loss_coefficient": 0.1
    },
    "experience_store": {
        "capacity": 10000,
        "overwrite_type": "rolling",
        "batch_size": -1,
        "replace": False
    },
    "sampler": {
        "rollout": {"batch_size": -1, "replace": False},
        "update": {"batch_size": 128, "replace": True}
    }
}


class MyACNet(DiscreteACNet):
    def forward(self, states, actor: bool = True, critic: bool = True):
        inputs = torch.from_numpy(np.asarray([st["model"] for st in states])).to(self.device)
        masks = torch.from_numpy(np.asarray([st["mask"] for st in states])).to(self.device)
        if len(inputs.shape) == 1:
            inputs = inputs.unsqueeze(dim=0)
        return (
            self.component["actor"](inputs) * masks if actor else None,
            self.component["critic"](inputs) if critic else None
        )


def get_ac_policy(mode="update"):
    ac_net = MyACNet(
        component={
            "actor": FullyConnectedBlock(**config["model"]["network"]["actor"]),
            "critic": FullyConnectedBlock(**config["model"]["network"]["critic"])
        },
        optim_option={
            "actor":  OptimOption(**config["model"]["optimization"]["actor"]),
            "critic": OptimOption(**config["model"]["optimization"]["critic"])
        } if mode != "inference" else None
    )
    if mode == "update":
        exp_store = ExperienceStore(**config["experience_store"]["update"])
        experience_sampler_kwargs = config["sampler"]["update"]
    else:
        exp_store = ExperienceStore(**config["experience_store"]["rollout" if mode == "inference" else "update"])
        experience_sampler_kwargs = config["sampler"]["rollout" if mode == "inference" else "update"]

    return ActorCritic(
        ac_net, ActorCriticConfig(**config["algorithm"]), exp_store,
        experience_sampler_cls=UniformSampler,
        experience_sampler_kwargs=experience_sampler_kwargs
    )


def get_ac_experiences(replay_buffer):
    def discount_cumsum(x, discount):
        """
        magic from rllab for computing discounted cumulative sums of vectors.
        
        Reference: https://github.com/openai/spinningup/blob/master/spinup/algos/pytorch/ppo/core.py
        """
        return scipy.signal.lfilter([1], [1, float(-discount)], x[::-1], axis=0)[::-1]

    rewards = np.array(replay_buffer["rewards"])
    cumsum_rewards = discount_cumsum(rewards, config["algorithm"]["reward_discount"])

    exp_set = ExperienceSet(
        replay_buffer["states"][:-1],
        replay_buffer["actions"][:-1],
        cumsum_rewards[:-1],
        replay_buffer["states"][1:],
        replay_buffer["info"][1:],
    )
    return exp_set
