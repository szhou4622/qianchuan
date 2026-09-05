import io
import json
import logging
import tempfile
from datetime import datetime, timedelta
import unittest
from pathlib import Path

from services.failure_report import _trace_evidence, sanitize
from utils.log import configure_logging
from utils.log_redaction import redact_text


class RuntimeLoggingTests(unittest.TestCase):
    def test_rotation_removes_only_excess_matching_backups(self):
        with tempfile.TemporaryDirectory(prefix='qcsckp-log-rotation-') as directory:
            logger = logging.Logger('isolated-rotation-test')
            logger.addHandler(logging.NullHandler())
            try:
                configure_logging(directory, logger)
                handler = next(h for h in logger.handlers if getattr(h, '_qcsckp_disk_sink', False))
                root = Path(directory)
                first = datetime(2020, 1, 1)
                for i in range(32):
                    (root / ('app.' + (first + timedelta(hours=4 * i)).strftime('%Y%m%d-%H'))).write_text('old', encoding='utf-8')
                unrelated = root / 'app.manual-backup'
                unrelated.write_text('keep', encoding='utf-8')
                self.assertEqual(2, len(handler.getFilesToDelete()))
                handler.doRollover()
                backups = [path for path in root.glob('app.*') if handler.extMatch.fullmatch(path.name[4:])]
                self.assertEqual(30, len(backups))
                self.assertTrue(unrelated.is_file())
                self.assertFalse((root / 'app.20200101-00').exists())
                self.assertTrue((root / 'app').is_file())
            finally:
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()

    def test_existing_handler_cannot_suppress_error_file_and_reinit_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix='qcsckp-log-') as directory:
            logger = logging.Logger('isolated-runtime-test')
            foreign = logging.StreamHandler(io.StringIO())
            logger.addHandler(foreign)
            try:
                configure_logging(directory, logger)
                configure_logging(directory, logger)
                self.assertIn(foreign, logger.handlers)
                self.assertEqual(1, sum(bool(getattr(h, '_qcsckp_disk_sink', False)) for h in logger.handlers))
                try:
                    raise RuntimeError('access_token=short-secret')
                except RuntimeError:
                    logger.exception('test failure app_secret=%s', 'arg-secret')
                content = (Path(directory) / 'app').read_text(encoding='utf-8')
                self.assertIn('ERROR', content)
                self.assertIn('RuntimeError', content)
                self.assertIn('Traceback', content)
                self.assertRegex(content, r'File "<local-path>/test_runtime_logging\.py", line \d+')
                frames = _trace_evidence(content)['frames']
                self.assertTrue(any(frame['file'] == 'test_runtime_logging.py' and frame['line'] > 0 for frame in frames))
                self.assertNotIn(str(Path(__file__).resolve().parent), content)
                self.assertEqual(1, content.count('test failure'))
                self.assertNotIn('short-secret', content)
                self.assertNotIn('arg-secret', content)
            finally:
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()

    def test_traceback_keeps_code_basename_but_not_private_paths_or_business_filenames(self):
        raw = (
            'Traceback (most recent call last):\n'
            '  File "C:\\Users\\PrivateName\\项目\\stop_worker.py", line 123, in run\n'
            '  File "/Users/OtherPrivate/app/api_client.py", line 456, in request\n'
            'RuntimeError: access_token=frame-secret\n'
            'business_file="C:\\Users\\PrivateName\\客户投放预算.xlsx"\n'
            'ordinary_script="C:\\Users\\PrivateName\\客户自定义脚本.py"\n'
        )
        safe = redact_text(raw)
        evidence = _trace_evidence(safe)
        self.assertEqual([
            {'file': 'stop_worker.py', 'line': 123},
            {'file': 'api_client.py', 'line': 456},
        ], evidence['frames'])
        self.assertIn('File "<local-path>/stop_worker.py", line 123', safe)
        for private in ('PrivateName', 'OtherPrivate', '项目', 'frame-secret', '客户投放预算.xlsx', '客户自定义脚本.py'):
            self.assertNotIn(private, safe)
            self.assertNotIn(private, json.dumps(evidence))

    def test_report_redacts_short_secrets_in_free_text_and_technical_tokens(self):
        raw = {
            'message': 'RuntimeError: access_token=ABC123 Cookie: sid=XYZ789',
            'payload': '{"app_secret": "shortsecret", "authorization": "Bearer smallbearer"}',
            'nested': {'api_key': 'smallkey', 'session_id': 'smallsession'},
            'detail': 'PrivateAccountName failed',
        }
        safe = sanitize(raw)
        text = json.dumps(safe)
        for secret in ('ABC123', 'XYZ789', 'shortsecret', 'smallbearer', 'smallkey', 'smallsession', 'PrivateAccountName'):
            self.assertNotIn(secret, text)
        self.assertIn('RuntimeError', safe['message']['technical_tokens'])
