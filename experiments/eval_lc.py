import sys
import os
import time
from datetime import datetime
import traceback
from collections import deque
import json

from absl import app, flags, logging

import numpy as np
import tensorflow as tf

import jax
import imageio

from flax.training import checkpoints
from jaxrl_m.vision import encoders
from jaxrl_m.agents import agents
from jaxrl_m.data.text_processing import text_processors

# bridge_data_robot imports
from widowx_envs.widowx_env import BridgeDataRailRLPrivateWidowX
from multicam_server.topic_utils import IMTopic

np.set_printoptions(suppress=True)

logging.set_verbosity(logging.WARNING)

FLAGS = flags.FLAGS

flags.DEFINE_multi_string(
    "checkpoint_weights_path", None, "Path to checkpoint", required=True
)
flags.DEFINE_multi_string(
    "checkpoint_config_path", None, "Path to checkpoint config JSON", required=True
)
flags.DEFINE_integer("im_size", None, "Image size", required=True)
flags.DEFINE_string("video_save_path", None, "Path to save video")
flags.DEFINE_integer("num_timesteps", 120, "num timesteps")
flags.DEFINE_bool("blocking", False, "Use the blocking controller")
flags.DEFINE_spaceseplist("initial_eep", None, "Initial position")
flags.DEFINE_integer("act_exec_horizon", 1, "Action sequence length")
flags.DEFINE_bool("deterministic", True, "Whether to sample action deterministically")

##############################################################################

STEP_DURATION = 0.2
NO_PITCH_ROLL = False
NO_YAW = False
STICKY_GRIPPER_NUM_STEPS = 1
WORKSPACE_BOUNDS = np.array([[0.1, -0.15, -0.1, -1.57, 0], [0.45, 0.25, 0.25, 1.57, 0]])
CAMERA_TOPICS = [IMTopic("/blue/image_raw")]
FIXED_STD = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

##############################################################################


def stack_obs(obs):
    dict_list = {k: [dic[k] for dic in obs] for k in obs[0]}
    return jax.tree_map(
        lambda x: np.stack(x), dict_list, is_leaf=lambda x: type(x) == list
    )


def load_checkpoint(checkpoint_weights_path, checkpoint_config_path):
    with open(checkpoint_config_path, "r") as f:
        config = json.load(f)

    # create encoder from wandb config
    encoder_def = encoders[config["encoder"]](**config["encoder_kwargs"])

    act_pred_horizon = config["dataset_kwargs"].get("act_pred_horizon")
    obs_horizon = config["dataset_kwargs"].get("obs_horizon")

    if act_pred_horizon is not None:
        example_actions = np.zeros((1, act_pred_horizon, 7), dtype=np.float32)
    else:
        example_actions = np.zeros((1, 7), dtype=np.float32)

    if obs_horizon is not None:
        example_obs = {
            "image": np.zeros(
                (1, obs_horizon, FLAGS.im_size, FLAGS.im_size, 3), dtype=np.uint8
            )
        }
    else:
        example_obs = {
            "image": np.zeros((1, FLAGS.im_size, FLAGS.im_size, 3), dtype=np.uint8)
        }

    example_batch = {
        "observations": example_obs,
        "goals": {"language": np.zeros((1, 512), dtype=np.float32)},
        "actions": example_actions,
    }

    # create agent from wandb config
    rng = jax.random.PRNGKey(0)
    rng, construct_rng = jax.random.split(rng)
    agent = agents[config["agent"]].create(
        rng=construct_rng,
        observations=example_batch["observations"],
        goals=example_batch["goals"],
        actions=example_batch["actions"],
        encoder_def=encoder_def,
        **config["agent_kwargs"],
    )

    # load action metadata from wandb
    action_proprio_metadata = config["bridgedata_config"]["action_proprio_metadata"]
    action_mean = np.array(action_proprio_metadata["action"]["mean"])
    action_std = np.array(action_proprio_metadata["action"]["std"])

    # hydrate agent with parameters from checkpoint
    agent = checkpoints.restore_checkpoint(checkpoint_weights_path, agent)

    def get_action(obs, goal_obs):
        nonlocal rng
        rng, key = jax.random.split(rng)
        action = jax.device_get(
            agent.sample_actions(obs, goal_obs, seed=key, argmax=FLAGS.deterministic)
        )
        action = action * action_std + action_mean
        return action

    text_processor = text_processors[config["text_processor"]](
        **config["text_processor_kwargs"]
    )

    return get_action, text_processor, obs_horizon


def main(_):
    assert len(FLAGS.checkpoint_weights_path) == len(FLAGS.checkpoint_config_path)

    # policies is a dict from run_name to get_action function
    policies = {}
    for checkpoint_weights_path, checkpoint_config_path in zip(
        FLAGS.checkpoint_weights_path, FLAGS.checkpoint_config_path
    ):
        assert tf.io.gfile.exists(checkpoint_weights_path), checkpoint_weights_path
        checkpoint_num = int(checkpoint_weights_path.split("_")[-1])
        run_name = checkpoint_config_path.split("/")[-1]
        policies[f"{run_name}-{checkpoint_num}"] = load_checkpoint(
            checkpoint_weights_path, checkpoint_config_path
        )

    if FLAGS.initial_eep is not None:
        assert isinstance(FLAGS.initial_eep, list)
        initial_eep = [float(e) for e in FLAGS.initial_eep]
        start_state = np.concatenate([initial_eep, [0, 0, 0, 1]])
    else:
        start_state = None

    # set up environment
    env_params = {
        "fix_zangle": 0.1,
        "move_duration": 0.2,
        "adaptive_wait": True,
        "move_to_rand_start_freq": 1,
        "override_workspace_boundaries": WORKSPACE_BOUNDS,
        "action_clipping": "xyz",
        "catch_environment_except": False,
        "start_state": start_state,
        "return_full_image": False,
        "camera_topics": CAMERA_TOPICS,
    }
    env = BridgeDataRailRLPrivateWidowX(env_params, fixed_image_size=FLAGS.im_size)

    instruction = None

    # instruction sampling loop
    while True:
        # ask for which policy to use
        if len(policies) == 1:
            policy_idx = 0
            input("Press [Enter] to start.")
        else:
            print("policies:")
            for i, name in enumerate(policies.keys()):
                print(f"{i}) {name}")
            policy_idx = int(input("select policy: "))

        policy_name = list(policies.keys())[policy_idx]
        get_action, text_processor, obs_horizon = policies[policy_name]

        # ask for new instruction
        if instruction is None:
            ch = "y"
        else:
            ch = input("New instruction? [y/n]")
        if ch == "y":
            instruction = text_processor.encode(input("Instruction?"))

        try:
            env.reset()
            env.start()
        except Exception as e:
            continue

        # move to initial position
        try:
            if FLAGS.initial_eep is not None:
                assert isinstance(FLAGS.initial_eep, list)
                initial_eep = [float(e) for e in FLAGS.initial_eep]
                env.controller().move_to_state(initial_eep, 0, duration=1.5)
                env._reset_previous_qpos()
        except Exception as e:
            continue

        # do rollout
        obs = env.current_obs()
        last_tstep = time.time()
        images = []
        t = 0
        if obs_horizon is not None:
            obs_hist = deque(maxlen=obs_horizon)
        # keep track of our own gripper state to implement sticky gripper
        is_gripper_closed = False
        num_consecutive_gripper_change_actions = 0
        try:
            while t < FLAGS.num_timesteps:
                if time.time() > last_tstep + STEP_DURATION or FLAGS.blocking:
                    image_obs = (
                        obs["image"]
                        .reshape(3, FLAGS.im_size, FLAGS.im_size)
                        .transpose(1, 2, 0)
                        * 255
                    ).astype(np.uint8)
                    obs = {"image": image_obs, "proprio": obs["state"]}
                    goal_obs = {"language": instruction}
                    if obs_horizon is not None:
                        if len(obs_hist) == 0:
                            obs_hist.extend([obs] * obs_horizon)
                        else:
                            obs_hist.append(obs)
                        obs = stack_obs(obs_hist)

                    last_tstep = time.time()

                    actions = get_action(obs, goal_obs)
                    if len(actions.shape) == 1:
                        actions = actions[None]
                    for i in range(FLAGS.act_exec_horizon):
                        action = actions[i]
                        action += np.random.normal(0, FIXED_STD)

                        # sticky gripper logic
                        if (action[-1] < 0.5) != is_gripper_closed:
                            num_consecutive_gripper_change_actions += 1
                        else:
                            num_consecutive_gripper_change_actions = 0

                        if (
                            num_consecutive_gripper_change_actions
                            >= STICKY_GRIPPER_NUM_STEPS
                        ):
                            is_gripper_closed = not is_gripper_closed
                            num_consecutive_gripper_change_actions = 0

                        action[-1] = 0.0 if is_gripper_closed else 1.0

                        # remove degrees of freedom
                        if NO_PITCH_ROLL:
                            action[3] = 0
                            action[4] = 0
                        if NO_YAW:
                            action[5] = 0

                        # perform environment step
                        obs, _, _, _ = env.step(
                            action, last_tstep + STEP_DURATION, blocking=FLAGS.blocking
                        )

                        # save image
                        images.append(image_obs)

                        t += 1
        except Exception as e:
            print(traceback.format_exc(), file=sys.stderr)

        # save video
        if FLAGS.video_save_path is not None:
            os.makedirs(FLAGS.video_save_path, exist_ok=True)
            curr_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            save_path = os.path.join(
                FLAGS.video_save_path,
                f"{curr_time}_{policy_name}_sticky_{STICKY_GRIPPER_NUM_STEPS}.mp4",
            )
            imageio.mimsave(save_path, images, fps=1.0 / STEP_DURATION * 3)


if __name__ == "__main__":
    app.run(main)
