import boto3
import logging

from multiprocessing import Process
from botocore.exceptions import ClientError
from typing import List, Dict, Any, Tuple

from . import Instance
from .instancemanager import InstanceManager
from ..utils import get_own_ip, random_str

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
        if config['cloud.aws.region'] is not None:
            self.region = config['cloud.aws.region'] 
        else:
            self.region = internal_conf['cloud.aws.region']
        
        if config['cloud.aws.secret_key'] and config['cloud.aws.access_key']:
            self.ec2_resource: boto3.session.Session.resource = boto3.resource('ec2',
                                                aws_access_key_id=config['cloud.aws.access_key'],
                                                aws_secret_access_key=config['cloud.aws.secret_key'],
                                                region_name=self.region)
            self.ec2_client = self.ec2_resource.meta.client

            
        else:
            # This will look for the env variables
            self.ec2_client: boto3.botocore.client.BaseClient = boto3.client('ec2', region=self.region)

        # ec2.Instance is of <class 'boto3.resources.factory.ec2.Instance'> but this cannot be
        # used for typing information as it is generated by the factory at runtime, I assume.
        self.workers: List[Any] = []
        self.message_queue: Any = None
        self.security_group_id: str = None
        self.worker_count: int = config['cloud.aws.worker_count']
        self.default_message_queue_ami = internal_conf['cloud.aws.message_queue_ami']
        self.default_worker_ami = internal_conf['cloud.aws.worker_ami']
        self.console_printer = context.console_printer
        self.timeout_ms = config['cloud.aws.timeout']


    def _provision_instances(self, timeout_ms, count=2, **kwargs) -> Tuple[Any, Any]:
        """Start and configure instances"""

        imageId = kwargs.get('imageId_queue', self.default_message_queue_ami)
        instanceType = 'm4.large'  # TODO pull from config
        message_queue = self.ec2_resource.create_instances(ImageId=imageId,
                                                                MinCount=1,
                                                                MaxCount=1,
                                                                InstanceType=instanceType)[0]

        # Workers
        imageId = kwargs.get('imageId_workers', self.default_worker_ami)
        # TODO pull from config
        instanceType = kwargs.get('instanceType', 't2.micro')

        return (message_queue, self.ec2_resource.create_instances(
            ImageId=imageId,
            MinCount=count,
            MaxCount=count,
            InstanceType=instanceType,
            SecurityGroupIds=[self.security_group_id]))

    def __map_ec2_instance(self, instance: Any) -> Instance:
        return Instance(instance.id, instance.public_ip_address, instance.image_id == self.default_message_queue_ami)
        

    def _get_running_instances(self) -> List[Instance]:
        return [self.__map_ec2_instance(self.message_queue)] + [self.__map_ec2_instance(i) for i in self.workers]

    def _terminate_instances(self, timeout_ms) -> None:
        """Terminate all instances managed by AWSTools"""
        instance_ids = [self.message_queue.id] + [instance.id for instance in self.workers]
        self.ec2_client.terminate_instances(
            InstanceIds=instance_ids)

    def _wait_for_instances(self):
        self.message_queue.wait_until_running()

        for instance in self.workers:
            instance.wait_until_running()

    def _run_start_scripts(self, timeout_ms) -> None:
        raise NotImplementedError

    def __enter__(self):
        """On enter, AWSTools prepares the AWS security group and spins up the required intances"""
        self.security_group_id = self._create_sec_group('csaopt_' + random_str(10))

        # TODO: put all required parameters into kwargs
        queue, workers = self._provision_instances(count=self.worker_count, timeout_ms=self.timeout_ms)

        self.message_queue = queue
        self.workers = workers

        self._wait_for_instances()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """On exit, AWSTools terminates the started instances and removes security groups"""
        self._terminate_instances(self.timeout_ms)
        for instance in self.workers:
            instance.wait_until_terminated()
        self.message_queue.wait_until_terminated()
        self._remove_sec_group(self.security_group_id)
    
    def _remove_sec_group(self, group_id: str) -> None:
        """Removes the security group created by CSAOpt"""

        if group_id is not None:
            try:
                self.ec2_client.delete_security_group(GroupId=group_id)
                logger.debug('Security group [{}] deleted'.format(group_id))
            except ClientError as e:
                logger.error('Could not remove security group: {}'.format(e))
        else:
            logger.warn('Cannot remove security group, because none was created. Skipping...')

    def _create_sec_group(self, name: str) -> str:
        self.own_external_ip = get_own_ip()
        response = self.ec2_client.describe_vpcs()
        vpc_id = response.get('Vpcs', [{}])[0].get('VpcId', '')

        try:
            response = self.ec2_client.create_security_group(
                GroupName=name,
                Description='Security Group for CSAOpt',
                VpcId=vpc_id)

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
                data
                ))
            
            return security_group_id
        except ClientError as e:
            logger.error('Could not create Security Group: {}', e)
            return None
