"""The acceptance rule of os/qemu-test.sh's update-check step (issue #35), evaluated on sample outputs: the function is extracted from the script and run under bash, so what is tested
is the text the QEMU run uses. The step asks the guest's update service whether there is an update and wants a DEFINITE answer: a successful reply (nothing newer, or one or more entries)
or one of the plain errors for "nothing published yet", "cannot reach the server" and "no source configured". It must reject anything else (access denied, a timeout, no output)."""
import re, subprocess, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = (HERE / 'qemu-test.sh').read_text()


def accepts(output):
    m = re.search(r'^update_check_ok\(\) \{.*?^\}', SCRIPT, re.S | re.M)
    assert m, 'qemu-test.sh has no update_check_ok function'
    r = subprocess.run(['bash', '-c', m.group(0) + '\nupdate_check_ok "$1"', 'x', output], capture_output=True, text=True)
    return r.returncode == 0


class UpdateCheckAcceptanceTests(unittest.TestCase):
    def test_a_successful_reply_is_a_definite_answer(self):
        self.assertTrue(accepts('a{sa{sv}}a{sa{sv}} 0 0'))                                              # nothing newer: what the guest says when its build is the newest
        self.assertTrue(accepts('a{sa{sv}}a{sa{sv}} 1 "20261016.3" 2 "version" s "0.2.0" "build" s "x" 0'))   # an update was found

    def test_the_three_plain_errors_are_still_definite_answers(self):
        for text in ('Call failed: no update has been published yet', 'Call failed: cannot reach the update server', 'Call failed: no update source is configured'):
            self.assertTrue(accepts(text), text)

    def test_anything_else_is_rejected(self):
        for text in ('', 'Access denied', 'Call failed: Connection timed out', 'Failed to call method: org.freedesktop.DBus.Error.ServiceUnknown', 'a{sa{sv}} 0', 'a{sa{sv}}a{sa{sv}}', 'busctl: command not found'):
            self.assertFalse(accepts(text), repr(text))

    def test_the_step_uses_the_function_once_and_the_old_inline_rule_is_gone(self):
        self.assertIn('update_check_ok "$(update_check_output)"', SCRIPT.replace('\\"', '"').replace('\\$', '$'))
        self.assertNotIn("== *'no update has been published yet'* ||", SCRIPT)


if __name__ == '__main__':
    unittest.main()
