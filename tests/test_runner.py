import pytest
import numpy as np

from context import Runner, ExecutionType, get_configs, docker_available

try:
    assert docker_available()

    class MockContext():
        def __init__(self):
            self.obj = {}

    # @pytest.mark.skip(reason='Worker image is not yet available on docker hub')
    def test_runner_langermann():
        internal_conf = get_configs('csaopt/internal/csaopt-internal.conf')
        ctx = {}
        ctx['internal_conf'] = internal_conf

        runner = Runner(['examples/ackley/ackley_opt.py'], ['examples/ackley/ackley.conf'], ctx)

        runner.run()
        if len(runner.failures) > 0:
            raise Exception('Runner had failures: %s' % runner.failures)

        assert runner.best_value == pytest.approx(0, abs=0.2)

except Exception:
    pass
