import types
import unittest

from hangar.boxlite import run_checked


class ExecutionStreamsTest(unittest.IsolatedAsyncioTestCase):
    async def test_drain_before_wait_and_decode_bytes(self):
        completed = set()

        class Execution:
            def stdout(self):
                return self.stream("stdout", b"ready\n")

            def stderr(self):
                return self.stream("stderr", b"notice\n")

            async def stream(self, name, content):
                yield content
                completed.add(name)

            async def wait(self):
                if completed != {"stdout", "stderr"}:
                    raise RuntimeError("REST streams were closed before output consumption")
                return types.SimpleNamespace(exit_code=0)

        class Box:
            async def exec(self, *_args, **_kwargs):
                return Execution()

        output = []
        await run_checked(Box(), "probe", log=output.append)
        self.assertCountEqual(output, ["ready\n", "notice\n"])
