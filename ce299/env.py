# =============================================================================
# @file   env.py
# @author Juanwu Lu
# @date   Dec-2-22
# =============================================================================
from __future__ import annotations

import math
import os
import sys
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import numpy as np
from gym.core import ActType, Env, ObsType
from gym.spaces import Box, Discrete
from ray.rllib.env.env_context import EnvContext

from ce299.typing import PathLike

# SUMO Traci
if 'SUMO_HOME' in os.environ:
    sys.path.append(os.path.join(os.environ['SUMO_HOME'], 'tools'))
    import sumolib
    import traci
else:
    sys.exit('Please declare envrionment variable "SUMO_HOME".')

ASSET_DIR = os.path.join(os.path.dirname(__file__), 'assets')


class CVI80VSLEnv(Env):
    """I80 Emeryville connected vehicle variable speed limit environment.

    This environment is associated with the variable speed limit control
    problem on Interstate I80, Emeryville, CA. Scenario is generated using
    NGSIM I80 Emeryville Dataset.

    - Action Space

        The action is a float array of shape `(1, )` with elements in range
        `[15.64, 29.06]`, corresponding to 35~65 mph.

    - Observation Space

        The observation is an array of shape `[2, H, W]`, where the first
        dimension is a heatmap of vehicle position given specific time and
        the second dimension is a timestep encoding.

    - Rewards

        The reward is the negative sum of mainline vehicle speed variation
        and mean time loss of all the vehicles at current time step.

    - Starting State

        The starting state is captured after warming up for 300 seconds.

    - Episode Termination

        The episode terminates after 3600 simulation seconds.

    - Arguments
    """
    metadata: Dict[str, Any] = {'render_modes': ['human']}

    def __init__(self, config: EnvContext) -> None:
        super().__init__()

        self.penetration_rate = config.get('penetration_rate', 0.0)
        assert 0.0 <= self.penetration_rate <= 1.0, ValueError(
            'Expect penetration rate to be between 0 and 1, ',
            f'but got {self.penetration_rate}.'
        )
        self.exp_name = config.get('exp_name', 'default')
        self.raster_length = config.get('raster_length', 20.0)
        self.step_interval: float = config.get('step_interval', 6.0)
        self.sumo_binary = 'sumo-gui' if config.get('gui', False) else 'sumo'
        self.sumo_cfg = os.path.join(ASSET_DIR, 'I80', 'i80.sumo.cfg')
        self.route_file = self.set_route()

        # Observation parameters
        self._net = sumolib.net.readNet(
            os.path.join(ASSET_DIR, 'I80', 'i80.net.xml'))
        self._obs_edges = [
            # Mainline edges
            'i80_upstream_n',
            'i80_weaving_n',
            'i80_weaving_ext_n',
            'i80_shrink_n',
            # Ramps
            'powell_on_ramp',
            'ashby_off_ramp'
        ]
        self._edge_left_grid_map = {}
        self._edge_right_grid_map = {}
        _grid = 0
        for edge_id in self._obs_edges:
            edge = self._net.getEdge(edge_id)
            if 'i80' in edge_id:
                self._edge_left_grid_map[edge_id] = _grid
                _grid += math.ceil(edge.getLength() / self.raster_length)
                self._edge_right_grid_map[edge_id] = _grid
            if edge_id == 'powell_on_ramp':
                self._edge_left_grid_map[edge_id] = \
                    self._edge_left_grid_map['i80_weaving_n'] - \
                    math.ceil(edge.getLength() / self.raster_length)
                self._edge_right_grid_map[edge_id] = \
                    self._edge_left_grid_map['i80_weaving_n']
            if edge_id == 'ashby_off_ramp':
                self._edge_left_grid_map[edge_id] = \
                    self._edge_right_grid_map['i80_weaving_ext_n']
                self._edge_right_grid_map[edge_id] = \
                    self._edge_right_grid_map['i80_weaving_ext_n'] + \
                    math.ceil(edge.getLength() / self.raster_length)
        self.obs_width = max(self._edge_right_grid_map.values())
        self.obs_height = max(
            self._net.getEdge(edge_id).getLaneNumber()
            for edge_id in self._obs_edges if 'i80' in edge_id
        ) + 1
        self.observation_space = Box(
            low=0.0, high=float('inf'),
            shape=(
                int(self.obs_height * self.step_interval), self.obs_width, 2
            )
        )

        # Action and Reward parameters
        if config.get('discrete', True):
            self.action_list = [
                15.64, 17.88, 20.12, 22.35, 24.59, 26.82, 29.06
            ]
            self.action_space = Discrete(n=len(self.action_list))
        else:
            self.action_list = None
            self.action_space = Box(low=15.64, high=29.06, shape=(1, ))

    def close(self) -> None:
        traci.close(False)

    def reset(self,
              *,
              seed: Optional[int] = None,
              options: Optional[Dict] = None) -> ObsType:
        super().reset(seed=seed, options=options)

        if seed is not None:
            sumo_cmd = [
                self.sumo_binary,
                '-c', self.sumo_cfg,
                '--route-files', self.route_file,
                '--start',
                '--seed', str(seed),
                '--quit-on-end'
            ]
        else:
            sumo_cmd = [
                self.sumo_binary,
                '-c', self.sumo_cfg,
                '--route-files', self.route_file,
                '--start',
                '--quit-on-end'
            ]

        traci.start(sumo_cmd, label='sim_' + str(time.time()))
        self.warm_up()

        curr_time = traci.simulation.getTime()
        obs, t_feat = [], []
        for time_step in range(math.floor(self.step_interval)):
            while traci.simulation.getTime() < curr_time + 1.0:
                traci.simulationStep()

            curr_obs = self.get_observation()
            obs.append(curr_obs)
            t_feat.append(np.ones_like(curr_obs) * time_step)
            curr_time += 1.0

        obs = np.concatenate(obs, axis=0)
        t_feat = np.concatenate(t_feat, axis=0)
        obs = np.stack([obs, t_feat]).transpose(1, 2, 0)

        return obs

    def step(self, action: ActType) -> Tuple[ObsType, float, bool, Dict]:
        """Take action to apply speed limit to all the connected vehicles."""
        if self.action_list is None:
            assert action.shape == self.action_space.shape, ValueError(
                f'Incosistent action shape, expect {self.action_space.shape}, '
                f'but got {action.shape}.'
            )
            # NOTE: Continuous VSL
            self.set_vsl(action)
        else:
            _action = self.action_list[action]
            self.set_vsl(_action)

        curr_time = traci.simulation.getTime()
        obs, t_feat = [], []
        reward = []
        for time_step in range(math.floor(self.step_interval)):
            while traci.simulation.getTime() < curr_time + 1.0:
                traci.simulationStep()

            curr_obs = self.get_observation()
            obs.append(curr_obs)
            t_feat.append(np.ones_like(curr_obs) * time_step)
            reward.append(self.get_reward())
            curr_time += 1.0

        obs = np.concatenate(obs, axis=0)
        t_feat = np.concatenate(t_feat, axis=0)
        obs = np.stack([obs, t_feat]).transpose(1, 2, 0)
        reward = np.mean(reward)  # Return the maximum reward in the interval
        done = traci.simulation.getTime() >= 3900

        if done:
            self.close()

        return obs, reward, done, {}

    def get_observation(self) -> np.ndarray:
        obs = np.zeros([self.obs_height, self.obs_width], 'float32')
        for edge_id in self._obs_edges:
            edge = self._net.getEdge(edge_id)
            edge_len = edge.getLength()
            num_lanes = edge.getLaneNumber()
            _left = self._edge_left_grid_map[edge_id]
            _right = self._edge_right_grid_map[edge_id]

            for idx in range(num_lanes):
                row_obs = np.zeros([1, _right - _left], 'float32')
                lane_id = '_'.join([edge_id, str(idx)])
                veh_ids = traci.lane.getLastStepVehicleIDs(lane_id)
                veh_pos = list(
                    map(
                        lambda x, eid=edge_id, elen=edge_len:
                        traci.vehicle.getDrivingDistance(x, eid, elen),
                        veh_ids
                    )
                )
                for v_id, v_pos in zip(veh_ids, veh_pos):
                    veh_len = traci.vehicle.getLength(v_id)
                    rear_grid = math.floor(v_pos / self.raster_length)
                    front_grid = math.ceil(v_pos / self.raster_length)
                    if rear_grid + 1 == front_grid:
                        row_obs[0, rear_grid] += 1
                    if rear_grid + 2 == front_grid:
                        ref = self.raster_length * (rear_grid + 1)
                        if rear_grid >= 0:
                            row_obs[0, rear_grid] = \
                                (ref - v_pos + veh_len / 2) / veh_len
                        if rear_grid + 1 <= _right:
                            row_obs[0, rear_grid + 1] = \
                                (v_pos + veh_len / 2 - ref) / veh_len

                if 'ramp' in edge_id:
                    obs[-1, _left:_right] = row_obs
                else:
                    obs[idx, _left:_right] = row_obs

        return obs

    def get_reward(self) -> float:
        # Harmonization: Minimize variation of mainline vehicles' speed
        var_reward = -np.var(
            [traci.vehicle.getSpeed(v) for v in self.mainline_vehicles]
        )

        # Efficiency: Minimize average waiting time of all vehicles
        wt_reward = -np.mean(
            [traci.vehicle.getTimeLoss(v) for v in self.vehicles]
        )

        return 0.6 * var_reward + 0.4 * wt_reward

    def set_route(self) -> PathLike:
        if not os.path.isdir(os.path.join(ASSET_DIR, 'tmp')):
            os.makedirs(os.path.join(ASSET_DIR, 'tmp'))

        my_tree = ET.parse(os.path.join(ASSET_DIR, 'I80', 'i80.rou.xml'))
        if self.penetration_rate > 0.0:
            my_root = my_tree.getroot()
            my_flow = my_root.findall('flow[@route="through"]')
            for flow in my_flow:
                flow_id = flow.get('id')
                vehs_per_hour = float(flow.get('vehsPerHour'))
                new_cv_vph = self.penetration_rate * vehs_per_hour
                new_vph = (1 - self.penetration_rate) * vehs_per_hour
                cv_flow: ET.Element = deepcopy(flow)
                flow.set('vehsPerHour', str(new_vph))
                cv_flow.set('vehsPerHour', str(new_cv_vph))
                cv_flow.set('id', flow_id + '_cv')
                cv_flow.set('type', 'cv_car')
                my_root.append(cv_flow)
        my_tree.write(os.path.join(ASSET_DIR, 'I80/tmp', 'i80.rou.xml'))

        return os.path.join(ASSET_DIR, 'I80/tmp', 'i80.rou.xml')

    def set_vsl(self, action: ActType) -> None:
        if isinstance(action, float):
            vsl = action
        elif isinstance(action, np.ndarray) and len(action.shape) == 1:
            vsl = action[0]
        else:
            raise ValueError(f'Invalid action value {action}!')

        for vehicle in self.vehicles:
            if 'cv' in vehicle:
                # traci.vehicle.setMaxSpeed(vehicle, vsl)
                traci.vehicle.setSpeed(vehicle, vsl)

    def warm_up(self) -> None:
        """Warm up simulation before getting the starting state."""
        while traci.simulation.getTime() <= \
                300 - math.floor(self.step_interval):
            traci.simulationStep()

    @property
    def mainline_vehicles(self) -> List[str]:
        ml_veh_ids = []
        for edge_id in self._obs_edges:
            if 'i80' not in edge_id:
                continue

            edge = self._net.getEdge(edge_id)
            for lane in edge.getLanes():
                ml_veh_ids += traci.lane.getLastStepVehicleIDs(lane.getID())

        return ml_veh_ids

    @property
    def vehicles(self) -> List[str]:
        veh_ids = []
        for edge_id in self._obs_edges:
            edge = self._net.getEdge(edge_id)
            for lane in edge.getLanes():
                veh_ids += traci.lane.getLastStepVehicleIDs(lane.getID())

        return veh_ids


if __name__ == '__main__':
    env = CVI80VSLEnv(penetration_rate=0.1, gui=True)
    obs, _ = env.reset(seed=42)
    done = False
    while not done:
        next_obs, rew, done, _, _ = env.step(env.action_space.sample())
        obs = next_obs
    env.close()
