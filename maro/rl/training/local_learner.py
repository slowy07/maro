# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import time
from collections import defaultdict
from os import getcwd
from typing import Dict, List, Union

from maro.rl.env_wrapper import AbsEnvWrapper
from maro.rl.exploration import AbsExploration
from maro.rl.policy import AbsPolicy
from maro.utils import Logger

from .early_stopper import AbsEarlyStopper


class LocalLearner:
    """Controller for single-threaded learning workflows.

    Args:
        env (AbsEnvWrapper): Environment wrapper instance to interact with a set of agents and collect experiences
            for policy updates.
        policies (List[AbsPolicy]): A set of named policies for inference.
        agent2policy (Dict[str, str]): Mapping from agent ID's to policy ID's. This is used to direct an agent's
            queries to the correct policy.
        num_episodes (int): Number of training episodes. Each training episode may contain one or more
            collect-update cycles, depending on how the implementation of the roll-out manager.
        num_steps (int): Number of environment steps to roll out in each call to ``collect``. Defaults to -1, in which
            case the roll-out will be executed until the end of the environment.
        exploration_dict (Dict[str, AbsExploration]): A set of named exploration schemes. Defaults to None.
        agent2exploration (Dict[str, str]): Mapping from agent ID's to exploration scheme ID's. This is used to direct
            an agent's query to the correct exploration scheme. Defaults to None.
        eval_schedule (Union[int, List[int]]): Evaluation schedule. If an integer is provided, the policies will
            will be evaluated every ``eval_schedule`` episodes. If a list is provided, the policies will be evaluated
            at the end of the training episodes given in the list. In any case, the policies will be evaluated
            at the end of the last training episode. Defaults to None, in which case the policies will only be
            evaluated after the last training episode.
        eval_env (AbsEnvWrapper): An ``AbsEnvWrapper`` instance for policy evaluation. If None, ``env`` will be used
            as the evaluation environment. Defaults to None.
        early_stopper (AbsEarlyStopper): Early stopper to stop the main training loop if certain conditions on the
            environment metrics are met following an evaluation episode. Default to None.
        log_env_summary (bool): If True, the ``summary`` property of the environment wrapper will be logged at the end of
            each episode. Defaults to True.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "LOCAL_ROLLOUT_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current working
            directory.
    """

    def __init__(
        self,
        env: AbsEnvWrapper,
        policies: List[AbsPolicy],
        agent2policy: Dict[str, str],
        num_episodes: int,
        num_steps: int = -1,
        exploration_dict: Dict[str, AbsExploration] = None,
        agent2exploration: Dict[str, str] = None,
        eval_schedule: Union[int, List[int]] = None,
        eval_env: AbsEnvWrapper = None,
        early_stopper: AbsEarlyStopper = None,
        log_env_summary: bool = True,
        log_dir: str = getcwd(),
    ):
        if num_steps == 0 or num_steps < -1:
            raise ValueError("num_steps must be a positive integer or -1")

        self._logger = Logger("LOCAL_LEARNER", dump_folder=log_dir)
        self.env = env
        self.eval_env = eval_env if eval_env else self.env

        # mappings between agents and policies
        self.policy_dict = {policy.name: policy for policy in policies}
        self._agent2policy = agent2policy
        self._policy = {agent_id: self.policy_dict[policy_id] for agent_id, policy_id in self._agent2policy.items()}
        self._agent_groups_by_policy = defaultdict(list)
        for agent_id, policy_id in agent2policy.items():
            self._agent_groups_by_policy[policy_id].append(agent_id)

        self.num_episodes = num_episodes
        self._num_steps = num_steps if num_steps > 0 else float("inf")

        # mappings between exploration schemes and agents
        self.exploration_dict = exploration_dict
        if exploration_dict:
            self._agent2exploration = agent2exploration
            self._exploration = {
                agent_id: self.exploration_dict[exploration_id]
                for agent_id, exploration_id in self._agent2exploration.items()
            }
            self._agent_groups_by_exploration = defaultdict(list)
            for agent_id, exploration_id in self._agent2exploration.items():
                self._agent_groups_by_exploration[exploration_id].append(agent_id)

        # evaluation schedule
        if eval_schedule is None:
            eval_schedule = []
        elif isinstance(eval_schedule, int):
            num_eval_schedule = num_episodes // eval_schedule
            eval_schedule = [eval_schedule * i for i in range(1, num_eval_schedule + 1)]

        self._eval_schedule = eval_schedule
        self._eval_schedule.sort()
        if not self._eval_schedule or num_episodes != self._eval_schedule[-1]:
            self._eval_schedule.append(num_episodes)
        self._eval_point_index = 0

        self.early_stopper = early_stopper

        self._log_env_summary = log_env_summary
        self._eval_ep = 0

    def run(self):
        """Entry point for executing a learning workflow."""
        for ep in range(1, self.num_episodes + 1):
            self._train(ep)
            if ep == self._eval_schedule[self._eval_point_index]:
                self._eval_point_index += 1
                self._evaluate()
                # early stopping check
                if self.early_stopper:
                    self.early_stopper.push(self.eval_env.summary)
                    if self.early_stopper.stop():
                        return

    def _train(self, ep: int):
        """Collect simulation data for training."""
        t0 = time.time()
        learning_time = 0
        num_experiences_collected = 0

        if self.exploration_dict:
            exploration_params = {
                tuple(agent_ids): self.exploration_dict[exploration_id].parameters
                for exploration_id, agent_ids in self._agent_groups_by_exploration.items()
            }
            self._logger.debug(f"Exploration parameters: {exploration_params}")

        self.env.reset()
        self.env.start()  # get initial state
        segment = 0
        while self.env.state:
            segment += 1
            for agent_id, exp in self._collect(ep, segment).items():
                self._policy[agent_id].on_experiences(exp)

        # update the exploration parameters if an episode is finished
        if self.exploration_dict:
            for exploration in self.exploration_dict.values():
                exploration.step()

        # performance details
        if self._log_env_summary:
            self._logger.info(f"ep {ep}: {self.env.summary}")

        self._logger.debug(
            f"ep {ep} summary - "
            f"running time: {time.time() - t0} "
            f"env steps: {self.env.step_index} "
            f"learning time: {learning_time} "
            f"experiences collected: {num_experiences_collected}"
        )

    def _evaluate(self):
        """Policy evaluation."""
        self._logger.info("Evaluating...")
        self._eval_ep += 1
        self.eval_env.reset()
        self.eval_env.start()  # get initial state
        while self.eval_env.state:
            action = {id_: self._policy[id_].choose_action(st) for id_, st in self.eval_env.state.items()}
            self.eval_env.step(action)

         # performance details
        self._logger.info(f"evaluation ep {self._eval_ep}: {self.eval_env.summary}")

    def _collect(self, ep, segment):
        start_step_index = self.env.step_index + 1
        steps_to_go = self._num_steps
        while self.env.state and steps_to_go:
            if self.exploration_dict:
                action = {
                    id_:
                        self._exploration[id_](self._policy[id_].choose_action(st))
                        if id_ in self._exploration else self._policy[id_].choose_action(st)
                    for id_, st in self.env.state.items()
                }
            else:
                action = {id_: self._policy[id_].choose_action(st) for id_, st in self.env.state.items()}
            self.env.step(action)
            steps_to_go -= 1

        self._logger.info(
            f"Roll-out finished for ep {ep}, segment {segment}"
            f"(steps {start_step_index} - {self.env.step_index})"
        )

        return self.env.get_experiences()
