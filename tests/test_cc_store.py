"""Offline storage tests: all storage paths are confined to temporary directories."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import cc_store as store


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cc-store-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        patcher = mock.patch.multiple(
            store,
            DATA_DIR=str(self.data),
            SESS_DIR=str(self.data / "sessions"),
            WS_DIR=str(self.data / "workspaces"),
            PROVIDERS_FILE=str(self.data / "providers.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sid = "s0123456789ab"

    def workspace_path(self, rel):
        return self.data / "workspaces" / self.sid / rel

    def test_upload_collision_preserves_original_bytes(self):
        original = bytes([0, 255]) + b"original\r\n"
        replacement = bytes([128]) + b"second" + bytes([0])
        first = store.save_upload(self.sid, "report.bin", original)
        second = store.save_upload(self.sid, "report.bin", replacement)
        self.assertEqual(self.workspace_path(first["path"]).read_bytes(), original)
        self.assertEqual(first, {"ok": True, "path": "uploads/report.bin", "size": len(original)})
        self.assertEqual(second, {"ok": True, "path": "uploads/report_1.bin", "size": len(replacement)})
        self.assertEqual(self.workspace_path(second["path"]).read_bytes(), replacement)

    def test_multiple_concurrent_collisions_preserve_every_payload(self):
        original = bytes(range(256))
        store.save_upload(self.sid, "shared.bin", original, sub="")
        count = 16
        barrier = threading.Barrier(count)

        def write(index):
            barrier.wait(timeout=10)
            if index % 2:
                payload = f"text-{index}-é".encode("utf-8")
                result = store.write_file(self.sid, "shared.bin", payload.decode("utf-8"))
            else:
                payload = bytes([index, 0, 255]) * 200
                result = store.save_upload(self.sid, "shared.bin", payload, sub="")
            return result, payload

        with ThreadPoolExecutor(max_workers=count) as executor:
            results = list(executor.map(write, range(count)))
        self.assertEqual(self.workspace_path("shared.bin").read_bytes(), original)
        self.assertEqual({r["path"] for r, _ in results},
                         {f"shared_{i}.bin" for i in range(1, count + 1)})
        for result, payload in results:
            self.assertEqual(self.workspace_path(result["path"]).read_bytes(), payload)
            self.assertEqual(result["size"], len(payload))

    def test_write_default_suffix_and_keyword_only_overwrite(self):
        first = store.write_file(self.sid, "nested/note.txt", "original")
        second = store.write_file(self.sid, "nested/note.txt", "é\n", overwrite=False)
        self.assertEqual(second, {"ok": True, "path": "nested/note_1.txt", "size": 3})
        self.assertEqual(self.workspace_path(first["path"]).read_bytes(), b"original")
        self.assertEqual(self.workspace_path(second["path"]).read_bytes(), "é\n".encode())
        changed = store.write_file(self.sid, "nested/note.txt", "new", overwrite=True)
        self.assertEqual(changed, {"ok": True, "path": "nested/note.txt", "size": 3})
        self.assertEqual(self.workspace_path(first["path"]).read_bytes(), b"new")
        self.assertEqual(self.workspace_path(second["path"]).read_bytes(), "é\n".encode())
        with self.assertRaises(TypeError):
            store.write_file(self.sid, "nested/note.txt", "bad", True)
        created = store.write_file(self.sid, "new.txt", "", overwrite=True)
        self.assertEqual(created["size"], 0)
        self.assertEqual(self.workspace_path("new.txt").read_bytes(), b"")

    def test_suffix_names_and_directory_collisions(self):
        for name, suffixed in (("archive.tar.gz", "archive.tar_1.gz"),
                               ("README", "README_1"), (".env", ".env_1")):
            with self.subTest(name=name):
                store.save_upload(self.sid, name, b"first")
                result = store.save_upload(self.sid, name, b"")
                self.assertEqual(result["path"], "uploads/" + suffixed)
                self.assertEqual(self.workspace_path(result["path"]).read_bytes(), b"")
        self.workspace_path("uploads/dir.txt").mkdir()
        result = store.save_upload(self.sid, "dir.txt", b"directory collision")
        self.assertEqual(result["path"], "uploads/dir_1.txt")

    def test_upload_basename_and_custom_subdirectory(self):
        result = store.save_upload(self.sid, "client/path/file.bin", b"payload", sub="a/b")
        self.assertEqual(result["path"], "a/b/file.bin")
        self.assertEqual(self.workspace_path(result["path"]).read_bytes(), b"payload")
        result = store.save_upload(self.sid, "", b"", sub="")
        self.assertEqual(result["path"], "upload.bin")

    def test_invalid_session_ids_rejected_before_storage_access(self):
        operations = (
            lambda sid: store._sess_path(sid), lambda sid: store._msg_path(sid),
            lambda sid: store.workspace_dir(sid), lambda sid: store.get_session(sid),
            lambda sid: store.update_session(sid, title="bad"),
            lambda sid: store.delete_session(sid),
            lambda sid: store.add_message(sid, "user", "bad"),
            lambda sid: store.get_messages(sid), lambda sid: store.list_files(sid),
            lambda sid: store.read_file(sid, "x"),
            lambda sid: store.write_file(sid, "x", "bad"),
            lambda sid: store.save_upload(sid, "x", b"bad"),
            lambda sid: store.delete_file(sid, "x"),
            lambda sid: store.search(sid, ""), lambda sid: store.search(sid, "x"),
        )
        invalid = (None, 123, "", ".", "..", "../outside", "/absolute", "a/b",
                   "a\\b", "a b", "a:drive", "x" + chr(0) + "y", "x\ny", "%2e%2e")
        # Invalid IDs must not even call _ensure or filesystem realpath resolution.
        with mock.patch.object(store, "_ensure") as ensure, \
                mock.patch.object(store.os.path, "realpath") as realpath:
            for sid in invalid:
                for index, operation in enumerate(operations):
                    with self.subTest(sid=sid, operation=index):
                        with self.assertRaisesRegex(ValueError, "invalid session id"):
                            operation(sid)
            ensure.assert_not_called()
            realpath.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_generated_and_safe_legacy_session_ids(self):
        session = store.create_session(title="offline")
        self.assertRegex(session["id"], r"^s[0-9a-f]{12}$")
        self.assertEqual(store.get_session(session["id"]), session)
        store.add_message(session["id"], "user", "hello")
        self.assertEqual(store.get_messages(session["id"])[0]["content"], "hello")
        self.assertEqual(store.get_session(session["id"])["message_count"], 1)
        for sid in (self.sid, "legacy_ID-1.2", "default"):
            self.assertEqual(store.workspace_dir(sid), str(self.data / "workspaces" / sid))
            self.assertEqual(store._sess_path(sid), str(self.data / "sessions" / (sid + ".json")))

    def test_safe_join_rejects_absolute_parent_and_symlink_escapes(self):
        base = self.root / "workspace"
        base.mkdir()
        outside = self.root / "workspace-other"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_bytes(b"untouched")
        (base / "escape").symlink_to(outside, target_is_directory=True)
        (base / "dangling").symlink_to(outside / "missing")
        bad_paths = (str(sentinel), "../workspace-other/keep.txt", "a/../../keep.txt",
                     "a/../keep.txt", "escape/keep.txt", "escape/new.txt", "dangling",
                     "C:/absolute", "C:relative", "\\host\\share", "x" + chr(0) + "y")
        for rel in bad_paths:
            with self.subTest(rel=rel):
                with self.assertRaises(ValueError):
                    store._safe_join(str(base), rel)
        (base / "inside").mkdir()
        (base / "link").symlink_to(base / "inside", target_is_directory=True)
        self.assertEqual(store._safe_join(str(base), "link/new.txt"), str(base / "link/new.txt"))
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertFalse((outside / "new.txt").exists())

    def test_public_file_operations_reject_escapes(self):
        base = Path(store.workspace_dir(self.sid))
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_bytes(b"untouched")
        (base / "escape").symlink_to(outside, target_is_directory=True)
        for rel in (str(sentinel), "../keep.txt", "escape/keep.txt"):
            with self.subTest(rel=rel):
                for overwrite in (False, True):
                    with self.assertRaises(ValueError):
                        store.write_file(self.sid, rel, "bad", overwrite=overwrite)
                self.assertFalse(store.read_file(self.sid, rel)["ok"])
                self.assertFalse(store.delete_file(self.sid, rel))
        for sub in (str(outside), "../outside", "escape"):
            with self.assertRaises(ValueError):
                store.save_upload(self.sid, "keep.txt", b"bad", sub=sub)
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_session_paths_reject_existing_symlink_escapes(self):
        store._ensure()
        outside = self.root / "outside"
        outside.mkdir()
        for name, operation in (
            (self.data / "sessions" / (self.sid + ".json"), store.get_session),
            (self.data / "sessions" / (self.sid + ".jsonl"), store.get_messages),
            (self.data / "workspaces" / self.sid, store.workspace_dir),
        ):
            name.symlink_to(outside)
            with self.assertRaises(ValueError):
                operation(self.sid)

    def test_each_suffix_is_checked_for_symlink_escape(self):
        store.save_upload(self.sid, "item.bin", b"original", sub="")
        sentinel = self.root / "outside.bin"
        sentinel.write_bytes(b"outside")
        self.workspace_path("item_1.bin").symlink_to(sentinel)
        with self.assertRaises(ValueError):
            store.save_upload(self.sid, "item.bin", b"bad", sub="")
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertEqual(self.workspace_path("item.bin").read_bytes(), b"original")

    def test_in_workspace_dangling_symlink_is_a_collision(self):
        store.workspace_dir(self.sid)
        target = self.workspace_path("target.bin")
        self.workspace_path("item.bin").symlink_to(target)
        result = store.save_upload(self.sid, "item.bin", b"new", sub="")
        self.assertEqual(result["path"], "item_1.bin")
        self.assertFalse(target.exists())
        self.assertTrue(self.workspace_path("item.bin").is_symlink())

    def test_non_collision_io_errors_are_not_retried(self):
        store.workspace_dir(self.sid)
        with mock.patch("builtins.open", side_effect=PermissionError("denied")) as opening:
            with self.assertRaises(PermissionError):
                store.save_upload(self.sid, "item.bin", b"new")
            self.assertEqual(opening.call_count, 1)


if __name__ == "__main__":
    unittest.main()
