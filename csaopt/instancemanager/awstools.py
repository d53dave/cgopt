import boto3
import logging

from botocore.exceptions import ClientError
from typing import List, Any, Tuple, Dict

from . import Instance
from .instancemanager import InstanceManager
from ..utils import get_own_ip, random_str, random_int

logger = logging.getLogger()


class AWSTools(InstanceManager):
    """The AWSTools class provides an abstraction over boto3 and EC2 for the use with CSAOpt

    It is intended to be used as a context manager, disposing of instances in it's __exit__()
    call.

    Create IAM credentials:
        * new IAM user with programmatic access
        * assign to a (potentially new) group with AmazonEC2FullAccess
        * write down and store the access key and secret key


    Boto3 will check these environment variables for credentials:

    Note:
        If the AWS credentials are not provided in the config file, boto3 will look into
        the following environment variables:
        * AWS_ACCESS_KEY_ID
        * AWS_SECRET_ACCESS_KEY

    """

    def __init__(self, context, config, internal_conf) -> None:
        self.region = config.get(
            'cloud.aws.region', internal_conf['cloud.aws.default_region'])

        if config.get('cloud.aws.secret_key', False) and config.get('cloud.aws.access_key', False):
            self.ec2_resource: boto3.session.Session.resource = boto3.resource(
                'ec2',
                aws_access_key_id=config['cloud.aws.access_key'],
                aws_secret_access_key=config['cloud.aws.secret_key'],
                region_name=self.region)

        else:
            # This will look for the env variables
            self.ec2_resource: boto3.session.Session.resource = boto3.resource(
                'ec2', region_name=self.region)

        self.ec2_client = self.ec2_resource.meta.client

        # ec2.Instance is of <class 'boto3.resources.factory.ec2.Instance'> but this cannot be
        # used as a type hint here because it is generated by the factory at runtime, I assume.
        self.workers: List[Any] = []
        self.broker: Any = None
        self.security_group_prefix: str = internal_conf.get(
            'cloud.aws.security_group_prefix', 'csaopt_')
        self.security_group_id: str = ''

        self.worker_count: int = config['cloud.aws.worker_count']

        worker_ami_key = 'cloud.aws.worker_ami'
        queue_ami_key = 'cloud.aws.message_queue_ami'

        self.broker_ami = config.get(
            queue_ami_key, internal_conf[queue_ami_key])
        self.worker_ami = config.get(
            worker_ami_key, internal_conf[worker_ami_key])

        self.console_printer = context.console_printer

        # TODO: this should be more fine-grained
        self.timeout_ms = config['cloud.aws.timeout']

        data_base = internal_conf['cloud.aws.userdata_rel_path']
        with open(data_base + '-broker.sh', 'rb') as queue_data, open(data_base + '-worker.sh', 'rb') as worker_data:
            self.user_data_scripts: Dict[str, bytes] = {
                # 'queue': base64.encodebytes(queue_data.read()).decode('ascii'),
                # 'worker': base64.encodebytes(worker_data.read()).decode('ascii')
                'queue': queue_data.read(),
                'worker': worker_data.read()
            }

        self.broker_port = random_int(49152, 65535)
        self.broker_password = random_str(32)

    def _provision_instances(self, timeout_ms, count=2, **kwargs) -> Tuple[Any, Any]:
        """Start and configure instances"""

        imageId = kwargs.get('imageId_queue', self.broker_ami)
        instanceType = 'm4.large'  # TODO pull from config
        message_queue = self.ec2_resource.create_instances(ImageId=imageId,
                                                           MinCount=1,
                                                           MaxCount=1,
                                                           UserData=self.user_data_scripts['queue'],
                                                           InstanceType=instanceType)[0]

        # Workers
        imageId = kwargs.get('imageId_workers', self.worker_ami)
        # TODO pull default from config
        instanceType = kwargs.get('instanceType', 't2.micro')

        workers = self.ec2_resource.create_instances(
            ImageId=imageId,
            MinCount=count,
            MaxCount=count,
            InstanceType=instanceType,
            UserData=self.user_data_scripts['worker'],
            SecurityGroupIds=[self.security_group_id])

        return message_queue, workers

    def __map_ec2_instance(self, instance: Any, is_broker=False, **kwargs) -> Instance:
        return Instance(instance.id, instance.public_ip_address, is_broker, **kwargs)

    def get_running_instances(self) -> Tuple[Instance, List[Instance]]:
        return (
            self.__map_ec2_instance(  # TODO: add port information
                instance=self.broker, is_broker=True, password=self.broker_password),
            [self.__map_ec2_instance(w) for w in self.workers]
        )

    def _terminate_instances(self, timeout_ms) -> None:
        """Terminate all instances managed by AWSTools"""
        instance_ids = [self.broker.id] + \
            [instance.id for instance in self.workers]
        self.ec2_client.terminate_instances(
            InstanceIds=instance_ids)

    def _wait_for_instances(self):
        self.broker.wait_until_running()

        for instance in self.workers:
            instance.wait_until_running()

    def _run_start_scripts(self, timeout_ms) -> None:
        raise NotImplementedError

    def __enter__(self):
        """On enter, AWSTools prepares the AWS security group and spins up the required intances"""
        self.console_printer.println('__enter__ AWSTools')
        self.security_group_id = self._create_sec_group(
            self.security_group_prefix + random_str(10))
        self.console_printer.println('SecGroup Created')

        # TODO: put all required parameters into kwargs
        queue, workers = self._provision_instances(
            count=self.worker_count, timeout_ms=self.timeout_ms)

        self.console_printer.println('Instances Created')
        self.broker = queue
        self.workers = workers

        self._wait_for_instances()
        self.console_printer.println('Instances Are Up')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """On exit, AWSTools terminates the started instances and removes security groups"""
        self._terminate_instances(self.timeout_ms)
        for instance in self.workers:
            instance.wait_until_terminated()
        self.broker.wait_until_terminated()
        self._remove_sec_group(self.security_group_id)
        return False

    def _remove_sec_group(self, group_id: str) -> None:
        """Removes the security group created by CSAOpt"""

        if group_id is not None:
            try:
                self.ec2_client.delete_security_group(GroupId=group_id)
                logger.debug('Security group [{}] deleted'.format(group_id))
            except ClientError as e:
                logger.error('Could not remove security group: {}'.format(e))
        else:
            logger.warn(
                'Cannot remove security group, because none was created. Skipping...')

    def _create_sec_group(self, name: str) -> str:
        self.own_external_ip = get_own_ip()
        # response = self.ec2_client.describe_vpcs()
        # vpc_id = response.get('Vpcs', [{}])[0].get('VpcId', '')
        try:
            response = self.ec2_client.create_security_group(
                GroupName=name,
                Description='Security Group for CSAOpt')

            security_group_id = response['GroupId']

            data = self.ec2_client.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[  # TODO: 80 and 22 are not needed here. Rather, these should be the zmq ports
                    {'IpProtocol': 'tcp',
                     'FromPort': 80,
                     'ToPort': 80,
                     'IpRanges': [{'CidrIp': '{}/0'.format(self.own_external_ip)}]},
                    {'IpProtocol': 'tcp',
                     'FromPort': 22,
                     'ToPort': 22,
                     'IpRanges': [{'CidrIp': '{}/0'.format(self.own_external_ip)}]}
                ])

            logger.debug('Authorized Security Group Ingress for CidrIp {} with result: {}'.format(
                self.own_external_ip,
                data))

            return security_group_id
        except ClientError as e:
            logger.error('Could not create Security Group: {}', e)
            raise
