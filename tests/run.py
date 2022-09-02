#!/usr/bin/env python
# Copyright (c) Alibaba, Inc. and its affiliates.

import argparse
import datetime
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from fnmatch import fnmatch
from multiprocessing.managers import BaseManager
from pathlib import Path
from turtle import shape
from unittest import TestResult, TextTestResult

import pandas
# NOTICE: Tensorflow 1.15 seems not so compatible with pytorch.
#         A segmentation fault may be raise by pytorch cpp library
#         if 'import tensorflow' in front of 'import torch'.
#         Puting a 'import torch' here can bypass this incompatibility.
import torch
import yaml

from modelscope.utils.logger import get_logger
from modelscope.utils.test_utils import set_test_level, test_level

logger = get_logger()


def test_cases_result_to_df(result_list):
    table_header = [
        'Name', 'Result', 'Info', 'Start time', 'Stop time',
        'Time cost(seconds)'
    ]
    df = pandas.DataFrame(
        result_list, columns=table_header).sort_values(
            by=['Start time'], ascending=True)
    return df


def statistics_test_result(df):
    total_cases = df.shape[0]
    # yapf: disable
    success_cases = df.loc[df['Result'] == 'Success'].shape[0]
    error_cases = df.loc[df['Result'] == 'Error'].shape[0]
    failures_cases = df.loc[df['Result'] == 'Failures'].shape[0]
    expected_failure_cases = df.loc[df['Result'] == 'ExpectedFailures'].shape[0]
    unexpected_success_cases = df.loc[df['Result'] == 'UnexpectedSuccesses'].shape[0]
    skipped_cases = df.loc[df['Result'] == 'Skipped'].shape[0]
    # yapf: enable

    if failures_cases > 0 or \
       error_cases > 0 or \
       unexpected_success_cases > 0:
        result = 'FAILED'
    else:
        result = 'SUCCESS'
    result_msg = '%s (Runs=%s,success=%s,failures=%s,errors=%s,\
    skipped=%s,expected failures=%s,unexpected successes=%s)' % (
        result, total_cases, success_cases, failures_cases, error_cases,
        skipped_cases, expected_failure_cases, unexpected_success_cases)

    print('Testing result summary.')
    print(result_msg)
    if result == 'FAILED':
        sys.exit(1)


def gather_test_suites_in_files(test_dir, case_file_list, list_tests):
    test_suite = unittest.TestSuite()
    for case in case_file_list:
        test_case = unittest.defaultTestLoader.discover(
            start_dir=test_dir, pattern=case)
        test_suite.addTest(test_case)
        if hasattr(test_case, '__iter__'):
            for subcase in test_case:
                if list_tests:
                    print(subcase)
        else:
            if list_tests:
                print(test_case)
    return test_suite


def gather_test_suites_files(test_dir, pattern):
    case_file_list = []
    for dirpath, dirnames, filenames in os.walk(test_dir):
        for file in filenames:
            if fnmatch(file, pattern):
                case_file_list.append(file)

    return case_file_list


def collect_test_results(case_results):
    result_list = [
    ]  # each item is Case, Result, Start time, Stop time, Time cost
    for case_result in case_results.successes:
        result_list.append(
            (case_result.test_full_name, 'Success', '', case_result.start_time,
             case_result.stop_time, case_result.time_cost))
    for case_result in case_results.errors:
        result_list.append(
            (case_result[0].test_full_name, 'Error', case_result[1],
             case_result[0].start_time, case_result[0].stop_time,
             case_result[0].time_cost))
    for case_result in case_results.skipped:
        result_list.append(
            (case_result[0].test_full_name, 'Skipped', case_result[1],
             case_result[0].start_time, case_result[0].stop_time,
             case_result[0].time_cost))
    for case_result in case_results.expectedFailures:
        result_list.append(
            (case_result[0].test_full_name, 'ExpectedFailures', case_result[1],
             case_result[0].start_time, case_result[0].stop_time,
             case_result[0].time_cost))
    for case_result in case_results.failures:
        result_list.append(
            (case_result[0].test_full_name, 'Failures', case_result[1],
             case_result[0].start_time, case_result[0].stop_time,
             case_result[0].time_cost))
    for case_result in case_results.unexpectedSuccesses:
        result_list.append((case_result.test_full_name, 'UnexpectedSuccesses',
                            '', case_result.start_time, case_result.stop_time,
                            case_result.time_cost))
    return result_list


def run_command_with_popen(cmd):
    with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            encoding='utf8') as sub_process:
        for line in iter(sub_process.stdout.readline, ''):
            sys.stdout.write(line)


def save_test_result(df, args):
    if args.result_dir is not None:
        file_name = str(int(datetime.datetime.now().timestamp() * 1000))
        os.umask(0)
        Path(args.result_dir).mkdir(mode=0o777, parents=True, exist_ok=True)
        Path(os.path.join(args.result_dir, file_name)).touch(
            mode=0o666, exist_ok=True)
        df.to_pickle(os.path.join(args.result_dir, file_name))


def run_command(cmd):
    logger.info('Running command: %s' % ' '.join(cmd))
    response = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        response.check_returncode()
        logger.info(response.stdout.decode('utf8'))
    except subprocess.CalledProcessError as error:
        logger.error(
            'stdout: %s, stderr: %s' %
            (response.stdout.decode('utf8'), error.stderr.decode('utf8')))


def install_packages(pkgs):
    cmd = [sys.executable, '-m', 'pip', 'install']
    for pkg in pkgs:
        cmd.append(pkg)

    run_command(cmd)


def install_requirements(requirements):
    for req in requirements:
        cmd = [
            sys.executable, '-m', 'pip', 'install', '-r',
            'requirements/%s' % req, '-f',
            'https://modelscope.oss-cn-beijing.aliyuncs.com/releases/repo.html'
        ]
        run_command(cmd)


def run_case_in_env(env_name, env, test_suite_env_map, isolated_cases,
                    result_dir):
    # install requirements and deps # run_config['envs'][env]
    if 'requirements' in env:
        install_requirements(env['requirements'])
    if 'dependencies' in env:
        install_packages(env['dependencies'])

    for test_suite_file in isolated_cases:  # run case in subprocess
        if test_suite_file in test_suite_env_map and test_suite_env_map[
                test_suite_file] == env_name:
            cmd = [
                'python',
                'tests/run.py',
                '--pattern',
                test_suite_file,
                '--result_dir',
                result_dir,
            ]
            run_command_with_popen(cmd)
        else:
            pass  # case not in run list.

    # run remain cases in a process.
    remain_suite_files = []
    for k, v in test_suite_env_map.items():
        if k not in isolated_cases and v == env_name:
            remain_suite_files.append(k)
    if len(remain_suite_files) == 0:
        return
    cmd = ['python', 'tests/run.py', '--result_dir', result_dir, '--suites']
    for suite in remain_suite_files:
        cmd.append(suite)
    run_command_with_popen(cmd)


def run_in_subprocess(args):
    # only case args.isolated_cases run in subporcess, all other run in a subprocess
    test_suite_files = gather_test_suites_files(
        os.path.abspath(args.test_dir), args.pattern)
    run_config = None
    isolated_cases = []
    test_suite_env_map = {}
    # put all the case in default env.
    for test_suite_file in test_suite_files:
        test_suite_env_map[test_suite_file] = 'default'

    if args.run_config is not None and Path(args.run_config).exists():
        with open(args.run_config) as f:
            run_config = yaml.load(f, Loader=yaml.FullLoader)
        if 'isolated' in run_config:
            isolated_cases = run_config['isolated']

        if 'envs' in run_config:
            for env in run_config['envs']:
                if env != 'default':
                    for test_suite in run_config['envs'][env]['tests']:
                        if test_suite in test_suite_env_map:
                            test_suite_env_map[test_suite] = env

    if args.subprocess:  # run all case in subprocess
        isolated_cases = test_suite_files

    with tempfile.TemporaryDirectory() as temp_result_dir:
        for env in set(test_suite_env_map.values()):
            run_case_in_env(env, run_config['envs'][env], test_suite_env_map,
                            isolated_cases, temp_result_dir)

        result_dfs = []
        result_path = Path(temp_result_dir)
        for result in result_path.iterdir():
            if Path.is_file(result):
                df = pandas.read_pickle(result)
                result_dfs.append(df)
        result_pd = pandas.concat(
            result_dfs)  # merge result of every test suite.
        print_table_result(result_pd)
        print_abnormal_case_info(result_pd)
        statistics_test_result(result_pd)


def get_object_full_name(obj):
    klass = obj.__class__
    module = klass.__module__
    if module == 'builtins':
        return klass.__qualname__
    return module + '.' + klass.__qualname__


class TimeCostTextTestResult(TextTestResult):
    """Record test case time used!"""

    def __init__(self, stream, descriptions, verbosity):
        self.successes = []
        return super(TimeCostTextTestResult,
                     self).__init__(stream, descriptions, verbosity)

    def startTest(self, test):
        test.start_time = datetime.datetime.now()
        test.test_full_name = get_object_full_name(
            test) + '.' + test._testMethodName
        self.stream.writeln('Test case:  %s start at: %s' %
                            (test.test_full_name, test.start_time))

        return super(TimeCostTextTestResult, self).startTest(test)

    def stopTest(self, test):
        TextTestResult.stopTest(self, test)
        test.stop_time = datetime.datetime.now()
        test.time_cost = (test.stop_time - test.start_time).total_seconds()
        self.stream.writeln(
            'Test case: %s stop at: %s, cost time: %s(seconds)' %
            (test.test_full_name, test.stop_time, test.time_cost))
        super(TimeCostTextTestResult, self).stopTest(test)

    def addSuccess(self, test):
        self.successes.append(test)
        super(TextTestResult, self).addSuccess(test)


class TimeCostTextTestRunner(unittest.runner.TextTestRunner):
    resultclass = TimeCostTextTestResult

    def run(self, test):
        return super(TimeCostTextTestRunner, self).run(test)

    def _makeResult(self):
        result = super(TimeCostTextTestRunner, self)._makeResult()
        return result


def gather_test_cases(test_dir, pattern, list_tests):
    case_list = []
    for dirpath, dirnames, filenames in os.walk(test_dir):
        for file in filenames:
            if fnmatch(file, pattern):
                case_list.append(file)

    test_suite = unittest.TestSuite()

    for case in case_list:
        test_case = unittest.defaultTestLoader.discover(
            start_dir=test_dir, pattern=case)
        test_suite.addTest(test_case)
        if hasattr(test_case, '__iter__'):
            for subcase in test_case:
                if list_tests:
                    print(subcase)
        else:
            if list_tests:
                print(test_case)
    return test_suite


def print_abnormal_case_info(df):
    df = df.loc[(df['Result'] == 'Error') | (df['Result'] == 'Failures')]
    for _, row in df.iterrows():
        print('Case %s run result: %s, msg:\n%s' %
              (row['Name'], row['Result'], row['Info']))


def print_table_result(df):
    df = df.loc[df['Result'] != 'Skipped']
    df = df.drop('Info', axis=1)
    formatters = {
        'Name': '{{:<{}s}}'.format(df['Name'].str.len().max()).format,
        'Result': '{{:<{}s}}'.format(df['Result'].str.len().max()).format,
    }
    with pandas.option_context('display.max_rows', None, 'display.max_columns',
                               None, 'display.width', None):
        print(df.to_string(justify='left', formatters=formatters, index=False))


def main(args):
    runner = TimeCostTextTestRunner()
    if args.suites is not None and len(args.suites) > 0:
        logger.info('Running: %s' % ' '.join(args.suites))
        test_suite = gather_test_suites_in_files(args.test_dir, args.suites,
                                                 args.list_tests)
    else:
        test_suite = gather_test_cases(
            os.path.abspath(args.test_dir), args.pattern, args.list_tests)
    if not args.list_tests:
        result = runner.run(test_suite)
        result = collect_test_results(result)
        df = test_cases_result_to_df(result)
        if args.result_dir is not None:
            save_test_result(df, args)
        else:
            print_table_result(df)
            print_abnormal_case_info(df)
            statistics_test_result(df)


if __name__ == '__main__':
    parser = argparse.ArgumentParser('test runner')
    parser.add_argument(
        '--list_tests', action='store_true', help='list all tests')
    parser.add_argument(
        '--pattern', default='test_*.py', help='test file pattern')
    parser.add_argument(
        '--test_dir', default='tests', help='directory to be tested')
    parser.add_argument(
        '--level', default=0, type=int, help='2 -- all, 1 -- p1, 0 -- p0')
    parser.add_argument(
        '--disable_profile', action='store_true', help='disable profiling')
    parser.add_argument(
        '--run_config',
        default=None,
        help='specified case run config file(yaml file)')
    parser.add_argument(
        '--subprocess',
        action='store_true',
        help='run all test suite in subprocess')
    parser.add_argument(
        '--result_dir',
        default=None,
        help='Save result to directory, internal use only')
    parser.add_argument(
        '--suites',
        nargs='*',
        help='Run specified test suites(test suite file list)')
    args = parser.parse_args()
    set_test_level(args.level)
    os.environ['REGRESSION_BASELINE'] = '1'
    logger.info(f'TEST LEVEL: {test_level()}')
    if not args.disable_profile:
        from utils import profiler
        logger.info('enable profile ...')
        profiler.enable()
    if args.run_config is not None or args.subprocess:
        run_in_subprocess(args)
    else:
        main(args)
