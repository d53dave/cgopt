__version__ = '0.1.0'
__appname__ = 'CSAOpt: Cloud based, GPU accelerated Simulated Annealing'

import asyncio
import logging
import shutil
import sys
import subprocess
import unicodedata
import re
import json

from pyhocon import ConfigTree
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.job import Job as ApJob
from asyncio.selector_events import BaseSelectorEventLoop
from typing import Dict, Optional, List, Any
from sty import fg, ef, rs, Rule, Render
from datetime import datetime, timedelta
from async_timeout import timeout

from .model_loader.model_loader import ModelLoader
from .model import Model
from .utils import get_configs
from .instancemanager.instancemanager import InstanceManager
from .instancemanager.awstools import AWSTools
from .instancemanager.local import Local
from .jobs.jobmanager import Job, JobManager, ExecutionType
from .broker import Broker

logger = logging.getLogger('csaopt.Runner')
fg.set_rule('csaopt_magenta', Rule(Render.rgb_fg, 199, 51, 147))


class ConsolePrinter:

    status_done = 'Done.'
    status_failed = 'Failed.'
    __ANSI_escape_re = re.compile(r'[\x1B|\x1b]\[[0-?]*[ -/]*[@-~]')

    def __init__(self, internal_config, log_level='info') -> None:
        self.spinner_idx = 0
        self.termsize = shutil.get_terminal_size((80, 20))
        self.last_line: str = ''
        self.has_scheduled_print: bool = False
        self.print_job: Optional[ApJob] = None
        self.scheduler: AsyncIOScheduler = AsyncIOScheduler()
        self.scheduler.start()
        self.spinner: List[str] = ['◣', '◤', '◥', '◢']
        self.log_level = log_level

        max_columns = internal_config.get('console.width_max')
        self.columns = internal_config.get('console.width_default')

        try:
            _, columns = subprocess.check_output(['stty', 'size']).split()
            if columns < max_columns:
                self.columns = columns
        except:
            logger.debug(
                'Could not get stty size, it seems there is no console available.')

    @staticmethod
    def _format_to_width(width: int, txt: str, status: str) -> str:
        txt_len = len(ConsolePrinter._remove_special_seqs(txt))
        status_len = len(ConsolePrinter._remove_special_seqs(status))
        if (txt_len + status_len) > width:
            return txt[0:(width - status_len - 4)] + '... ' + status

        return txt + ''.join([' '] * (width - status_len - txt_len)) + status

    @staticmethod
    def _remove_special_seqs(s):
        no_ansi = ConsolePrinter.__ANSI_escape_re.sub('', s)
        no_c = ''.join(c for c in no_ansi if unicodedata.category(c)[0] != 'C')
        return no_c

    def print(self, txt: str) -> None:
        self.spinner_idx = (self.spinner_idx + 1) % len(self.spinner)
        sys.stdout.write(txt + rs.all)
        sys.stdout.flush()
        self.last_line = txt

    def println(self, txt: str) -> None:
        self.print(txt + rs.all + '\n')

    def print_magenta(self, txt: str) -> None:
        self.print(fg.csaopt_magenta + txt)

    def print_with_spinner(self, txt: str) -> None:
        self.scheduler.remove_all_jobs()
        self.last_line = txt

        self.print_job = self.scheduler.add_job(
            lambda: self.print('\r' + ConsolePrinter._format_to_width(
                self.columns,
                txt,
                fg.csaopt_magenta + self.spinner[self.spinner_idx] + '    ')),
            'interval',
            seconds=0.42,
            id='print_job',
            max_instances=1,
            next_run_time=datetime.now() + timedelta(milliseconds=50))  # run in 50ms, then periodically

    def spinner_success(self) -> None:
        # If log level < warn, just re-print with 'Done.'
        # Truncate to console width to fit message
        if self.log_level == 'info':
            if self.print_job is not None:
                self.print_job.pause()
                self.print_job.remove()
            self.scheduler.remove_all_jobs()
            self.println(ConsolePrinter._format_to_width(
                self.columns,
                self.last_line[0:self.columns -
                               len(ConsolePrinter.status_done)],
                fg.green + ConsolePrinter.status_done))

    def spinner_failure(self) -> None:
        # If log level < warn, just re-print with 'Failed.'
        # Truncate to console width to fit message
        if self.log_level == 'info':
            self.scheduler.remove_all_jobs()
            self.println(ConsolePrinter._format_to_width(
                self.columns,
                self.last_line[0:self.columns -
                               len(ConsolePrinter.status_failed)],
                fg.red + ConsolePrinter.status_failed))


class Runner:

    def __init__(self, model_paths: List[str], conf_paths: List[str], invocation_options: Dict[str, Any]) -> None:
        internal_conf = invocation_options['internal_conf']

        self.console_printer = ConsolePrinter(internal_conf)
        self.conf_paths = conf_paths
        self.model_paths = model_paths
        self.invocation_options = invocation_options
        self.loop = asyncio.get_event_loop()
        self.models: List[Model] = []
        self.failures: List[str] = []

        self.console_printer.print_magenta(
            ef.bold + 'Welcome to CSAOpt v{}\n\n'.format(__version__))

    def _get_instance_manager(self, context, conf, internal_conf) -> InstanceManager:
        if conf['cloud.disabled'] is True:
            return Local(conf, internal_conf)

        cloud_platform = conf['cloud.platform']
        if cloud_platform == 'aws':
            return AWSTools(context, conf, internal_conf)
        # elif cloud_platform == 'gcp' etc...
            # return GCPTools()
        else:
            raise AttributeError('Cloud platform ' +
                                 cloud_platform + ' unrecognized.')

    def duplicate_cloud_configs(self, configs):
        # TODO this needs to be clearly stated in documentation
        for config in configs:
            if config.get('cloud', None) is not None:
                cloud_conf = config['cloud']
                break
        else:
            raise AssertionError('No cloud configuration found')

        for config in configs:
            config['cloud'] = cloud_conf

    async def _run_async(self, loop):
        printer = self.console_printer
        printer.print_with_spinner('Loading Config')
        try:
            configs = [get_configs(conf_path) for conf_path in self.conf_paths]
            self.duplicate_cloud_configs(configs)

            internal_conf = self.invocation_options['internal_conf']

            ctx = Context(printer, configs, internal_conf)
        except Exception as e:
            printer.spinner_failure()
            self.failures.append('Error while loading config: ' + str(e))
            raise e
        printer.spinner_success()

        printer.print_with_spinner('Loading Models')
        for idx, model_path in enumerate(self.model_paths):
            configs[idx]['model']['path'] = model_path
            logger.debug('Loading model {}'.format(model_path))
            loader = ModelLoader(configs[idx], internal_conf)
            self.models.insert(idx, loader.get_model())
            logger.debug('Models loaded succesfully.')
        printer.spinner_success()

        # Get cloud config, create instance manager
        self.cloud_config = configs[0]

        if self.cloud_config['cloud.disabled'] is True:
            start_msg = 'Starting local instances with docker'
        else:
            start_msg = 'Starting instances on {}'.format(
                self.cloud_config['cloud.platform'].upper())

        printer.print_with_spinner(start_msg)

        with self._get_instance_manager(ctx, self.cloud_config, internal_conf) as instancemanager:
            printer.spinner_success()
            printer.print_with_spinner(
                'Waiting for broker to come online')

            broker_instance, workers = instancemanager.get_running_instances()

            queue_ids: List[str] = []
            for worker in workers:
                if 'queue_id' in worker.props:
                    queue_ids.append(worker.props['queue_id'])

            assert len(
                queue_ids) > 0, 'There should be at least one worker running'
            async with timeout(30) as async_timeout:
                broker: Broker = Broker(
                    host=str(broker_instance.public_ip), port=broker_instance.port, queue_ids=queue_ids)
                printer.spinner_success()

            if async_timeout.expired:
                printer.spinner_failure()
                raise TimeoutError(
                    'Timed out waiting for broker to come online')

            jobmanager = JobManager(ctx, broker, self.models, configs)

            for worker_id in (await jobmanager.wait_for_worker_join()):
                printer.println(
                    'Worker {} joined'.format(worker_id))

            printer.print_with_spinner('Deploying model')
            try:
                await jobmanager.deploy_model()
            except Exception:
                msg = 'An exception occured during model deployment.'
                logger.exception(msg)
                printer.spinner_failure()
                self.failures.append(msg)
                return

            printer.spinner_success()

            await asyncio.sleep(0.8)

            printer.print_with_spinner(
                'Running Simulated Annealing')

            await asyncio.sleep(1)
            # TODO: this needs timeouts
            jobs: List[Job] = await jobmanager.submit()

            await jobmanager.wait_for_results()
            printer.spinner_success()

            printer.print_with_spinner('Retrieving results')
            printer.spinner_success()

            printer.print_with_spinner('Scanning for best result')
            # TODO: determine which worker reported this result
            len(jobs)
            printer.spinner_success()

            # printer.println('Evaluated: {} State: {}'.format(val_min, best_res))

            # if configs[0]['TODO']:
            #     printer.print_with_spinner('Saving best result to file: TODO')
            #     try:
            #         job.write_files()
            #         printer.spinner_success()
            #     except Exception as e:
            #         self.failures.append('Could not write to file: {}'.format(e))
            #         printer.spinner_failure()

    def run(self) -> None:
        """

        """

        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._run_async(loop))
        loop.close()

        if self.failures:
            self.console_printer.println(
                fg.red + 'It seems there have been errors. 🌩')
        else:
            self.console_printer.println(fg.green + 'All done. ✨')

    def cancel(self) -> None:
        pass


class Context:
    def __init__(self,
                 console_printer: ConsolePrinter,
                 configs: ConfigTree,
                 internal_config: ConfigTree) -> None:
        self.console_printer: ConsolePrinter = console_printer
        self.configs = configs
        self.internal_config = internal_config
