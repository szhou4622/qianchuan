import os
import re
import unittest
from unittest.mock import patch

from services import device_identity as identity


FEATURES = {
    "BOARD_UUID": "4C4C4544-0038-4D10-804A-CAC04F593633",
    "SYSTEM_DISK_SERIAL": " WD-WX42A1234567 ",
    "WINDOWS_MACHINE_GUID": "6f9619ff-8b86-d011-b42d-00c04fc964ff",
    "CPU_ID": "BFEBFBFF000906EA",
    "CPU_VENDOR": "GenuineIntel",
    "CPU_NAME": "Intel(R) Core(TM) Processor",
}


class DeviceIdentityTests(unittest.TestCase):
    def test_device_code_is_versioned_stable_and_valid(self):
        first = identity.generate_device_code(FEATURES)
        second = identity.generate_device_code(dict(reversed(list(FEATURES.items()))))
        self.assertEqual(first, second)
        self.assertRegex(first, r"^MC1-(?:[A-F0-9]{4}-){3}[A-F0-9]{4}$")
        self.assertTrue(identity.validate_device_code(first))

    def test_checksum_rejects_single_character_change(self):
        code = identity.generate_device_code(FEATURES)
        replacement = "A" if code[-1] != "A" else "B"
        self.assertFalse(identity.validate_device_code(code[:-1] + replacement))
        self.assertFalse(identity.validate_device_code("MC2-0000-0000-0000-0000"))

    def test_invalid_and_default_hardware_values_are_filtered(self):
        code = identity.generate_device_code(
            {
                "BOARD_UUID": "00000000-0000-0000-0000-000000000000",
                "SYSTEM_DISK_SERIAL": "To be filled by O.E.M.",
                "WINDOWS_MACHINE_GUID": FEATURES["WINDOWS_MACHINE_GUID"],
                "CPU_ID": "FFFFFFFFFFFFFFFF",
            }
        )
        expected = identity.generate_device_code(
            {"WINDOWS_MACHINE_GUID": FEATURES["WINDOWS_MACHINE_GUID"]}
        )
        self.assertEqual(expected, code)

    def test_network_and_computer_name_are_never_identity_inputs(self):
        base = identity.generate_device_code(FEATURES)
        noisy = identity.generate_device_code(
            {
                **FEATURES,
                "MAC_ADDRESS": "00-11-22-33-44-55",
                "IP_ADDRESS": "192.168.1.10",
                "COMPUTER_NAME": "DESKTOP-CHANGED",
            }
        )
        self.assertEqual(base, noisy)

    def test_single_windows_reader_failure_keeps_other_features(self):
        if os.name != "nt":
            self.skipTest("Windows hardware reader")
        with patch.object(
            identity,
            "_read_windows_cim_features",
            side_effect=RuntimeError("CIM unavailable"),
        ), patch.object(
            identity,
            "_read_windows_machine_guid",
            return_value=FEATURES["WINDOWS_MACHINE_GUID"],
        ):
            collected = identity._collect_raw_device_features()
        self.assertEqual(
            re.sub(r"[^A-Z0-9]+", "", FEATURES["WINDOWS_MACHINE_GUID"].upper()),
            collected["WINDOWS_MACHINE_GUID"],
        )
        self.assertTrue(identity.validate_device_code(identity.generate_device_code(collected)))

    def test_macos_uses_platform_serial_board_model_and_cpu_without_network_values(self):
        mac_features = {
            "MAC_PLATFORM_UUID": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
            "MAC_HARDWARE_SERIAL": "C02TESTSERIAL",
            "MAC_BOARD_ID": "Mac-1234567890ABCDEF",
            "MAC_MODEL": "Mac15,7",
            "MAC_CPU_BRAND": "Apple M3 Pro",
            "MAC_CPU_FAMILY": "458787763",
        }
        with patch.object(identity, "_platform_name", return_value="darwin"), patch.object(
            identity,
            "_read_macos_features",
            return_value=mac_features,
        ):
            collected = identity._collect_raw_device_features()
        self.assertEqual(set(mac_features), set(collected))
        code = identity.generate_device_code(collected)
        self.assertTrue(identity.validate_device_code(code))
        self.assertEqual(code, identity.generate_device_code(dict(reversed(list(collected.items())))))

    def test_macos_single_source_failure_does_not_crash_collector(self):
        with patch.object(identity, "_platform_name", return_value="darwin"), patch.object(
            identity,
            "_read_macos_features",
            side_effect=RuntimeError("ioreg unavailable"),
        ):
            self.assertEqual({}, identity._collect_raw_device_features())

    def test_macos_native_reader_parses_intel_and_apple_silicon_fields(self):
        ioreg = """
        \"IOPlatformUUID\" = \"AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE\"
        \"IOPlatformSerialNumber\" = \"C02TESTSERIAL\"
        \"board-id\" = <4d61632d31323334>
        """
        with patch.object(
            identity,
            "_run_text",
            side_effect=[ioreg, "Mac15,7", "Apple M3 Pro", "458787763"],
        ):
            result = identity._read_macos_features()
        self.assertEqual(
            "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
            result["MAC_PLATFORM_UUID"],
        )
        self.assertEqual("C02TESTSERIAL", result["MAC_HARDWARE_SERIAL"])
        self.assertEqual("4d61632d31323334", result["MAC_BOARD_ID"])
        self.assertEqual("Mac15,7", result["MAC_MODEL"])
        self.assertEqual("Apple M3 Pro", result["MAC_CPU_BRAND"])

    def test_all_invalid_features_fail_closed_without_raw_values(self):
        with self.assertRaises(identity.DeviceIdentityError) as caught:
            identity.generate_device_code(
                {
                    "BOARD_UUID": "00000000-0000-0000-0000-000000000000",
                    "SYSTEM_DISK_SERIAL": "Unknown",
                }
            )
        self.assertEqual("未取得可用的设备特征", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
