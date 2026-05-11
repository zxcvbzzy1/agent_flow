import pytest
from infra.tool.builtin.system import SystemTool


@pytest.fixture
def tool():
    return SystemTool(working_directory="/Users/zxcvbzzy1/Desktop/项目/agent_flow/temp")


class TestAuditHighRiskCommand:

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /",
            "sudo rm -rf /",
            "shutdown now",
        ]
    )
    def test_detect_high_risk_commands(self, tool, cmd):
        is_safe, message = tool.audit_high_risk_command(cmd)

        assert not is_safe

    @pytest.mark.parametrize(
        "cmd",
        [
            "ls -la",
            "echo hello",
            "cat test.txt",
        ]
    )
    def test_allow_safe_commands(self, tool, cmd):
        is_safe, message = tool.audit_high_risk_command(cmd)
        assert is_safe

    @pytest.mark.parametrize(
        "cmd",
        [
            " RM -RF / ",
            "sudo   rm   -rf   /",
            "bash -c 'rm -rf /'",
        ]
    )
    def test_detect_bypass_variants(self, tool, cmd):
        is_safe, _ = tool.audit_high_risk_command(cmd)
        assert not is_safe

    @pytest.mark.parametrize(
        "cmd",
        [
            "cd ../../",
            "bash -c 'cd ../../'",
            "cp ./test.txt ../",
        ]
    )
    def test_reject_high_risk_paths(self, tool, cmd):
        is_safe, _ = tool.audit_working_directory(cmd)
        assert not is_safe



if __name__ == "__main__":
    pytest.main([__file__])
