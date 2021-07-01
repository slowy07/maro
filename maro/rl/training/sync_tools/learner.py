# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import time
from os import getcwd
from typing import List, Union

from maro.utils import Logger

from ..policy_manager.policy_manager import AbsPolicyManager
from .early_stopper import AbsEarlyStopper
from .rollout_manager import AbsRolloutManager


class Learner:
    """Main controller for learning.

    This should be used in multi-process or distributed settings where either the policy manager or the roll-out
    manager has a distributed architecture. For pure local learning workflows, using this may cause pitfalls such
    as duplicate experience storage. Use ``LocalLearner`` instead.

    Args:
        policy_manager (AbsPolicyManager): An ``AbsPolicyManager`` instance that controls policy updates.
        rollout_manager (AbsRolloutManager): An ``AbsRolloutManager`` instance that controls simulation data
            collection.
        num_episodes (int): Number of training episodes. Each training episode may contain one or more
            collect-update cycles, depending on the implementation of the roll-out manager.
        eval_schedule (Union[int, List[int]]): Evaluation schedule. If an integer is provided, the policies will
            will be evaluated every ``eval_schedule`` episodes. If a list is provided, the policies will be evaluated
            at the end of the training episodes given in the list. In any case, the policies will be evaluated
            at the end of the last training episode. Defaults to None, in which case the policies will only be
            evaluated after the last training episode.
        early_stopper (AbsEarlyStopper): Early stopper to stop the main training loop if certain conditions on the
            environment metric are met following an evaluation episode. Default to None.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "LEARNER" will be created at init time
            and this directory will be used to save the log files generated by it. Defaults to the current working
            directory.
        end_of_episode_kwargs: Keyword arguments for custom end-of-episode processing.
    """
    def __init__(
        self,
        policy_manager: AbsPolicyManager,
        rollout_manager: AbsRolloutManager,
        num_episodes: int,
        eval_schedule: Union[int, List[int]] = None,
        early_stopper: AbsEarlyStopper = None,
        log_dir: str = getcwd(),
        **end_of_episode_kwargs
    ):
        self.logger = Logger("LEARNER", dump_folder=log_dir)
        self.policy_manager = policy_manager
        self.rollout_manager = rollout_manager

        self.num_episodes = num_episodes

        # evaluation schedule
        if eval_schedule is None:
            self._eval_schedule = []
        elif isinstance(eval_schedule, int):
            num_eval_schedule = num_episodes // eval_schedule
            self._eval_schedule = [eval_schedule * i for i in range(1, num_eval_schedule + 1)]
        else:
            self._eval_schedule = eval_schedule
            self._eval_schedule.sort()
            if not self._eval_schedule or num_episodes != self._eval_schedule[-1]:
                self._eval_schedule.append(num_episodes)

        self.logger.info(f"Policy will be evaluated at the end of episodes {self._eval_schedule}")
        self._eval_point_index = 0

        self.early_stopper = early_stopper

        self._end_of_episode_kwargs = end_of_episode_kwargs
        self._last_step_set = {}

    def run(self):
        """Entry point for executing a learning workflow."""
        for ep in range(1, self.num_episodes + 1):
            self._train(ep)
            if ep == self._eval_schedule[self._eval_point_index]:
                self._eval_point_index += 1
                env_metric_dict = self.rollout_manager.evaluate(ep, self.policy_manager.get_state())
                # performance details
                self.logger.info(f"Evaluation result: {env_metric_dict}")
                # early stopping check
                if self.early_stopper:
                    for env_metric in env_metric_dict.values():
                        self.early_stopper.push(env_metric)
                        if self.early_stopper.stop():
                            return

        if hasattr(self.rollout_manager, "exit"):
            self.rollout_manager.exit()

        if hasattr(self.policy_manager, "exit"):
            self.policy_manager.exit()

    def _train(self, ep: int):
        collect_time = policy_update_time = num_experiences_collected = 0
        segment = 0
        self.rollout_manager.reset()
        while not self.rollout_manager.episode_complete:
            segment += 1
            # experience collection
            policy_state_dict = self.policy_manager.get_state()
            self.policy_manager.reset_update_status()
            policy_version = self.policy_manager.version
            tc0 = time.time()
            exp_by_policy = self.rollout_manager.collect(ep, segment, policy_state_dict, policy_version)
            collect_time += time.time() - tc0
            tu0 = time.time()
            self.policy_manager.on_experiences(exp_by_policy)
            policy_update_time += time.time() - tu0
            num_experiences_collected += sum(exp.size for exp in exp_by_policy.values())

        # performance details
        self.logger.info(
            f"ep {ep} summary - "
            f"experiences collected: {num_experiences_collected} "
            f"experience collection time: {collect_time} "
            f"policy update time: {policy_update_time}"
        )

        self.end_of_episode(ep, **self._end_of_episode_kwargs)

    def end_of_episode(self, ep: int, **kwargs):
        """Custom end-of-episode processing is implemented here."""
        pass
