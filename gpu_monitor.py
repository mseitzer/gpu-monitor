#!/usr/bin/env python3
"""Script to check the state of GPU servers

This script is most useful in conjunction with an ssh-key, so a password does
not have to be entered for each SSH connection.
"""
import argparse
import logging
import os
import pwd
import subprocess
import sys
import time
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import partial
from logging import debug, info, error

# Default timeout in seconds after which SSH stops trying to connect
DEFAULT_SSH_TIMEOUT = 30

# Default timeout in seconds after which remote commands are interrupted
DEFAULT_CMD_TIMEOUT = 50

# Default server file
DEFAULT_SERVER_FILE = 'servers.txt'
SERVER_FILE_PATH = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])),
                                DEFAULT_SERVER_FILE)

# Default cpu affinities file for tasksetting
DEFAULT_TASKSET_FILE = 'cpu_affinities.json'
SERVER_TASKSET_PATH = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])),
                                DEFAULT_TASKSET_FILE)

NULL_FUNCTION = lambda *args, **kwargs : None

parser = argparse.ArgumentParser(description='Check state of GPU servers')
parser.add_argument('-v', '--verbose', action='store_true',
                    help='Be verbose')
parser.add_argument('-l', '--list', action='store_true', help='Show used GPUs')
parser.add_argument('-d', '--daemon', action='store_true',
                    help='Loop')
parser.add_argument('-t', '--taskset', action='store_true', help='Use Taskset to set CPU-GPU Affinities')
parser.add_argument('-f', '--finger', action='store_true',
                    help='Attempt to resolve user names to real names')
parser.add_argument('-m', '--me', action='store_true',
                    help='Show only GPUs used by current user')
parser.add_argument('-u', '--user', help='Shows only GPUs used by a user')
parser.add_argument('-s', '--ssh-user', default=None,
                    help='Username to use to connect with SSH')
parser.add_argument('--ssh-timeout', default=DEFAULT_SSH_TIMEOUT,
                    help='Timeout in seconds after which SSH stops to connect')
parser.add_argument('--cmd-timeout', default=DEFAULT_CMD_TIMEOUT,
                    help=('Timeout in seconds after which nvidia-smi '
                          'is interrupted'))
parser.add_argument('--server-file', default=SERVER_FILE_PATH,
                    help='File with addresses of servers to check')
parser.add_argument('--taskset-file', default=SERVER_TASKSET_PATH,
                    help='File with cpu affinities information if using tasksetting functionality')
parser.add_argument('servers', nargs='*', default=[],
                    help='Servers to probe')

# SSH command
SSH_CMD = ('ssh -o "ConnectTimeout={ssh_timeout}" {server} '
           'timeout {cmd_timeout}')

# Command for running nvidia-smi locally
NVIDIASMI_CMD = 'nvidia-smi -q -x'

# Command for running nvidia-smi remotely
REMOTE_NVIDIASMI_CMD = '{} {}'.format(SSH_CMD, NVIDIASMI_CMD)

# Command for running ps locally
PS_CMD = 'ps -o pid= -o ruser= -p {pids}'

# Command for tasksetting locally
TASKSET_CMD = 'taskset -cp {cpus} {pid}'

# Command for running ps remotely
REMOTE_PS_CMD = '{} {}'.format(SSH_CMD, PS_CMD)

# Command for tasksetting remotely
REMOTE_TASKSET_CMD = '{} {}'.format(SSH_CMD, TASKSET_CMD)

# Command for getting real names remotely
# See https://stackoverflow.com/a/38235661
REAL_NAMES_CMD = """<<-"EOF"
import pwd
for user in [{users}]:
    try:
        print(pwd.getpwnam(user).pw_gecos)
    except KeyError:
        print('Unknown')
EOF
"""
REMOTE_REAL_NAMES_CMD = '{} python - {}'.format(SSH_CMD, REAL_NAMES_CMD)


def run_command(cmd):
    debug('Running command: "{}"'.format(cmd))

    try:
        res = subprocess.check_output(cmd, shell=True)
    except subprocess.TimeoutExpired as e:
        debug(('Command timeouted with output "{}", '
               'and stderr "{}"'.format(e.output.decode('utf-8'), e.stderr)))
        return None
    except subprocess.CalledProcessError as e:
        debug(('Command failed with exit code {}, output "{}", '
               'and stderr "{}"'.format(e.returncode,
                                        e.output.decode('utf-8'),
                                        e.stderr)))
        return None

    return res


def run_nvidiasmi_local():
    res = run_command(NVIDIASMI_CMD)
    return ET.fromstring(res) if res is not None else None


def run_nvidiasmi_remote(server, ssh_timeout, cmd_timeout):
    cmd = REMOTE_NVIDIASMI_CMD.format(server=server,
                                      ssh_timeout=ssh_timeout,
                                      cmd_timeout=cmd_timeout)
    res = run_command(cmd)
    return ET.fromstring(res) if res is not None else None


def run_ps_local(pids):
    cmd = PS_CMD.format(pids=','.join(pids))
    res = run_command(cmd)
    return res.decode('ascii') if res is not None else None

def run_taskset_local(cpus, pid):
    cpus = [str(x) for x in cpus]
    cmd = TASKSET_CMD.format(cpus=','.join(cpus), pid=pid)
    res = run_command(cmd)
    return res.decode('ascii') if res is not None else None

def run_ps_remote(server, pids, ssh_timeout, cmd_timeout):
    cmd = REMOTE_PS_CMD.format(server=server,
                               pids=','.join(pids),
                               ssh_timeout=ssh_timeout,
                               cmd_timeout=cmd_timeout)
    res = run_command(cmd)
    return res.decode('ascii') if res is not None else None


def run_taskset_remote(server, cpus, pid, ssh_timeout, cmd_timeout):
    cpus = [str(x) for x in cpus]
    cmd = REMOTE_TASKSET_CMD.format(server=server,
                               pid=pid,
                               cpus=','.join(cpus),
                               ssh_timeout=ssh_timeout,
                               cmd_timeout=cmd_timeout)
    res = run_command(cmd)
    return res.decode('ascii') if res is not None else None



def get_real_names_local(users):
    real_names_by_users = {}
    for user in users:
        try:
            real_names_by_users[user] = pwd.getpwnam(user).pw_gecos
        except KeyError:
            pass
    return defaultdict(lambda: 'Unknown', real_names_by_users)


def get_real_names_remote(server, users, ssh_timeout, cmd_timeout):
    users_str = ','.join(('\'{}\''.format(user) for user in users))
    cmd = REMOTE_REAL_NAMES_CMD.format(server=server,
                                       users=users_str,
                                       ssh_timeout=ssh_timeout,
                                       cmd_timeout=cmd_timeout)
    res = run_command(cmd)
    if res is not None:
        res = res.decode('utf-8')
        real_names_by_users = {user: s.strip()
                               for user, s in zip(users, res.split('\n'))}
        return defaultdict(lambda: 'Unknown', real_names_by_users)
    else:
        return None


def get_users_by_pid(ps_output):
    users_by_pid = {}
    for line in ps_output.strip().split('\n'):
        pid, user = line.split()
        users_by_pid[pid] = user

    return users_by_pid


def get_gpu_infos(nvidiasmi_output):
    gpus = nvidiasmi_output.findall('gpu')

    gpu_infos = []
    for idx, gpu in enumerate(gpus):
        model = gpu.find('product_name').text
        processes = gpu.findall('processes')[0]
        pids = [process.find('pid').text for process in processes]
        gpu_infos.append({'idx': idx, 'model': model, 'pids': pids})

    return gpu_infos


def print_free_gpus(server, gpu_infos):
    free_gpus = [info for info in gpu_infos if len(info['pids']) == 0]

    if len(free_gpus) == 0:
        info('Server {}: No free GPUs :('.format(server))
    else:
        info('Server {}:'.format(server))
        for gpu_info in free_gpus:
            info('\tGPU {}, {}'.format(gpu_info['idx'], gpu_info['model']))


def print_gpu_infos(server, gpu_infos, run_ps, run_taskset,
                    run_get_real_names, filter_by_user=None,
                    translate_to_real_names=False, cpu_affinities={}):
    pids = [pid for gpu_info in gpu_infos for pid in gpu_info['pids']]
    if len(pids) > 0:
        ps = run_ps(pids=pids)
        if ps is None:
            error('Could not reach {} or error running ps'.format(server))
            return

        users_by_pid = get_users_by_pid(ps)
    else:
        users_by_pid = {}

    if server in cpu_affinities.keys():
        for gpu_info in gpu_infos:
            for pid in gpu_info["pids"]:
                gpu_cpus=cpu_affinities[server]["affinities"][str(gpu_info["idx"])]
                taskset = run_taskset(cpus=gpu_cpus, pid=pid)

    if translate_to_real_names:
        all_users = set((users_by_pid[pid] for gpu_info in gpu_infos
                         for pid in gpu_info['pids']))
        real_names_by_users = run_get_real_names(users=all_users)

    info('Server {}:'.format(server))
    for gpu_info in gpu_infos:
        users = set((users_by_pid[pid] for pid in gpu_info['pids']))
        if filter_by_user is not None and filter_by_user not in users:
            continue

        if len(gpu_info['pids']) == 0:
            status = 'Free'
        else:
            if translate_to_real_names:
                users = ['{} ({})'.format(user, real_names_by_users[user])
                         for user in users]

            status = 'Used by {}'.format(', '.join(users))

        info('\tGPU {} ({}): {}'.format(gpu_info['idx'],
                                        gpu_info['model'],
                                        status))


def main(args):
    
    logging.basicConfig(format='%(message)s',
                        level=logging.DEBUG if args.verbose else logging.INFO)

    if len(args.servers) == 0:
        try:
            debug('Using server file {}'.format(args.server_file))
            with open(args.server_file, 'r') as f:
                servers = (s.strip() for s in f.readlines())
                args.servers = [s for s in servers if s != '']
        except OSError as e:
            error('Could not open server file {}'.format(args.server_file))
            return

    try:
        debug('Using taskset file {}'.format(args.taskset_file))
        with open(args.taskset_file, 'r') as f:
            cpu_affinities = json.load(f)
    except OSError as e:
        error('Could not open server file {}'.format(args.server_file))
        cpu_affinities = {}
       
    if len(args.servers) == 0:
        error(('No GPU servers to connect to specified.\nPut addresses in '
               'the server file or specify them manually as an argument'))
        return

    if args.ssh_user is not None:
        args.servers = ['{}@{}'.format(args.ssh_user, server)
                        for server in args.servers]
    if args.me:
        if args.ssh_user is not None:
            args.user = args.ssh_user
        else:
            args.user = pwd.getpwuid(os.getuid()).pw_name
    if args.user or args.finger:
        args.list = True

    for server in args.servers:
        if server == '.' or server == 'localhost' or server == '127.0.0.1':
            run_nvidiasmi = run_nvidiasmi_local
            run_ps = run_ps_local
            run_taskset = run_taskset_local
            run_get_real_names = get_real_names_local
        else:
            run_nvidiasmi = partial(run_nvidiasmi_remote,
                                    server=server,
                                    ssh_timeout=args.ssh_timeout,
                                    cmd_timeout=args.cmd_timeout)
            run_ps = partial(run_ps_remote,
                             server=server,
                             ssh_timeout=args.ssh_timeout,
                             cmd_timeout=args.cmd_timeout)
            run_taskset = partial(run_taskset_remote,
                             server=server,
                             ssh_timeout=args.ssh_timeout,
                             cmd_timeout=args.cmd_timeout)
            run_get_real_names = partial(get_real_names_remote,
                                         server=server,
                                         ssh_timeout=args.ssh_timeout,
                                         cmd_timeout=args.cmd_timeout)

        run_taskset = NULL_FUNCTION if args.taskset is False else run_taskset

        nvidiasmi = run_nvidiasmi()
        if nvidiasmi is None:
            error(('Could not reach {} or '
                   'error running nvidia-smi').format(server))
            continue

        gpu_infos = get_gpu_infos(nvidiasmi)

        if args.list or args.taskset:
            print_gpu_infos(server, gpu_infos, run_ps, run_taskset,
                            run_get_real_names, filter_by_user=args.user,
                            translate_to_real_names=args.finger,
                            cpu_affinities=cpu_affinities)
        else:
            print_free_gpus(server, gpu_infos)


if __name__ == '__main__':
    args = parser.parse_args(sys.argv[1:])
    if args.daemon:
        while True:
          main(args)
          time.sleep(15)
    else:
          main(args)

