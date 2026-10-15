import json
import os
from pathlib import Path
import subprocess
import tempfile
import hashlib
import socket
import time
import threading
import uuid
import unittest
from unittest.mock import patch

from neoxider_agents.enforcement import (
    ScopeRequest, UnsafeScope, EnforcementUnavailable, validate_request,
    capabilities, prepare, launch)
from neoxider_agents.windows_enforcement import probe


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="neoxider-enforce-test-")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.file = self.workspace / "real.txt"
        self.file.write_text("original", encoding="utf-8")
        self.request = ScopeRequest(str(self.workspace), ("real.txt",))

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_file_and_directory_preflight_is_read_only(self):
        folder = self.workspace / "safe"
        folder.mkdir()
        (folder / "unicode-Р РЋР вЂљР РЋРЎвЂњР РЋР С“.txt").write_text("x", encoding="utf-8")
        request = ScopeRequest(str(self.workspace), ("real.txt", "safe"))
        self.assertEqual(validate_request(request), request)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "original")

    def test_no_launch_even_if_prerequisites_are_reported_present(self):
        with patch("subprocess.Popen") as process:
            with self.assertRaises(EnforcementUnavailable) as caught:
                launch(self.request, ["cmd", "/c", "exit"], network=True,
                       fallback=lambda: self.fail("fallback called"))
        process.assert_not_called()
        self.assertEqual(caught.exception.code, "enforced-backend-unavailable")
        self.assertFalse(caught.exception.evidence["enforced_write_scope"])

    def test_prepare_does_not_change_acl_or_source(self):
        before = self.file.stat()
        with self.assertRaises(EnforcementUnavailable):
            prepare(self.request)
        after = self.file.stat()
        self.assertEqual((before.st_mode, before.st_mtime_ns, before.st_size),
                         (after.st_mode, after.st_mtime_ns, after.st_size))
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), ["real.txt"])

    def test_unsupported_and_escape_semantics_rejected(self):
        for value in ("../real.txt", "/outside", "C:/outside", "safe/**",
                      "real.txt:stream", "\\\\server\\share", "./", ".",
                      "real.txt.", "real.txt ", "NUL", "CON.txt", "COM1"):
            with self.subTest(value=value):
                with self.assertRaises(UnsafeScope):
                    validate_request(ScopeRequest(str(self.workspace), (value,)))

    def test_existing_only_duplicate_and_capability_validation(self):
        for request in (ScopeRequest(str(self.workspace), ("absent",)),
                        ScopeRequest(str(self.workspace), ("real.txt", "real.txt")),
                        ScopeRequest(str(self.workspace), []),
                        ScopeRequest(str(self.workspace), ("real.txt",), network=1),
                        ScopeRequest("relative", ("real.txt",))):
            with self.subTest(request=request):
                with self.assertRaises(UnsafeScope):
                    validate_request(request)

    def test_real_hardlink_and_nested_hardlink_rejected(self):
        alias = self.workspace / "alias.txt"
        os.link(str(self.file), str(alias))
        with self.assertRaises(UnsafeScope) as caught:
            validate_request(self.request)
        self.assertEqual(caught.exception.code, "hardlink")
        folder = self.workspace / "folder"
        folder.mkdir()
        os.link(str(self.file), str(folder / "nested.txt"))
        with self.assertRaises(UnsafeScope):
            validate_request(ScopeRequest(str(self.workspace), ("folder",)))

    def test_real_directory_junction_or_symlink_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "linked"
        if os.name == "nt":
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                            str(link), str(outside)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(UnsafeScope) as caught:
                validate_request(ScopeRequest(str(self.workspace), ("linked",)))
            self.assertEqual(caught.exception.code, "link-or-reparse")
        finally:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()

    def test_structured_probe_never_claims_supported(self):
        evidence = probe()
        self.assertFalse(evidence["supported"])
        self.assertTrue(evidence["reason"])
        self.assertIn("everyone-writable-outside-denial", evidence["missing_acceptance"])
        json.dumps(capabilities())

    def test_refusal_is_not_cached_across_path_replacement(self):
        self.file.unlink()
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        os.link(str(outside), str(self.file))
        with self.assertRaises(UnsafeScope) as caught:
            prepare(self.request)
        self.assertEqual(caught.exception.code, "hardlink")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")


@unittest.skipUnless(os.name == "nt" and os.environ.get("AGENT_ENFORCEMENT_FIXTURE_BIN"),
                     "Real Windows fixture gate requires AGENT_ENFORCEMENT_FIXTURE_BIN; release acceptance must run it.")
class WindowsSessionTests(unittest.TestCase):
    def setUp(self):
        from neoxider_agents.windows_enforcement import WindowsConsoleSession
        self.Session = WindowsConsoleSession
        self.temp = tempfile.TemporaryDirectory(prefix="neoxider-native-scope-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.private_parent=self.root / "private"
        self.private_parent.mkdir()
        (self.source / "writable").mkdir()
        for name in ("allowed.txt", "protected.txt"):
            (self.source / name).write_text("original", encoding="utf-8")
        self.request = ScopeRequest(str(self.source), ("allowed.txt", "writable"))
        self.exe = Path(os.environ["AGENT_ENFORCEMENT_FIXTURE_BIN"]).absolute()
        self.pin = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.guard_pin = hashlib.sha256((self.source / "protected.txt").read_bytes()).hexdigest()
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
            self.assertEqual(session.evidence["profile_delete_hresult"], 0)
        self.temp.cleanup()

    def session(self):
        value = self.Session.prepare(self.request,
            {"fixture.exe": (str(self.exe), self.pin),
             "guard.txt": (str(self.source / "protected.txt"), self.guard_pin)},
             "fixture.exe", str(self.private_parent))
        self.sessions.append(value)
        return value

    def test_native_snapshot_stdio_and_exact_file_denials(self):
        from neoxider_agents.windows_enforcement import _Native
        native = _Native()
        original_security = {name: native.security_fingerprint(self.source / name)
                             for name in ("allowed.txt", "protected.txt")}
        session = self.session()
        outside = self.root / "outside.txt"
        session.launch([str(outside)])
        session.stdin.write(b"NEOXIDER_NATIVE_STDIO\n")
        session.stdin.close()
        self.assertEqual(session.wait(10), 0)
        output = session.stdout.read().decode()
        for expected in ("genuine_appcontainer=True", "win32k_enabled=True",
                         "stdin_echo=NEOXIDER_NATIVE_STDIO", "allowed_write=True",
                         "protected_write=False", "outside_write=False"):
            self.assertIn(expected, output)
        self.assertEqual((session.workspace / "allowed.txt").read_text(), "changed")
        self.assertEqual((session.workspace / "protected.txt").read_text(), "original")
        self.assertFalse(outside.exists())
        self.assertTrue(all((self.source / name).read_text() == "original"
                            for name in ("allowed.txt", "protected.txt")))
        self.assertEqual(original_security, {name: native.security_fingerprint(self.source / name)
                                           for name in original_security})

    def test_native_detached_child_and_forced_mitigation_downgrade_stay_restricted(self):
        for forced in (False, True):
            with self.subTest(forced=forced):
                session = self.session()
                receipt = session.temp / "child.txt"
                outside = self.root / "child-outside.txt"
                argv = ["--child-parent", str(receipt), str(outside)]
                if forced:
                    argv.append("--off")
                session.launch(argv)
                session.stdin.close()
                self.assertEqual(session.wait(10), 0)
                text = receipt.read_text()
                for expected in ("genuine_appcontainer=1", "win32k=1", "outside_write=0"):
                    self.assertIn(expected, text)
                self.assertIn("allowed_write=1", text)
                self.assertIn("restricting_sid=" + session._restriction_text, text)
                self.assertIn("restricting_count=1", text)
                self.assertFalse(outside.exists())

    def outside_fixture(self, name, grant):
        from neoxider_agents.windows_enforcement import _Native
        native = _Native()
        path = self.root / name
        path.write_text("UNCHANGED_OUTSIDE")
        c, w = native.c, native.w
        sd = native.descriptor("D:P(A;;GA;;;OW)(A;;GA;;;WD)" + grant + "S:(ML;;NW;;;LW)")
        try:
            present, defaulted = w.BOOL(), w.BOOL()
            dacl, sacl = c.c_void_p(), c.c_void_p()
            native.check(native.GetSecurityDescriptorDacl(sd,c.byref(present),c.byref(dacl),c.byref(defaulted)))
            native.check(native.GetSecurityDescriptorSacl(sd,c.byref(present),c.byref(sacl),c.byref(defaulted)))
            for flags, da, sa in ((4|0x80000000,dacl,None),(0x10,None,sacl)):
                code = native.SetNamedSecurityInfoW(str(path),1,flags,None,None,da,sa)
                self.assertEqual(code, 0)
        finally:
            native.LocalFree(sd)
        return path

    def test_native_ambient_package_grants_denied_in_real_child(self):
        from neoxider_agents.windows_enforcement import _Native
        native = _Native()
        for name, grant in (("aap", "(A;;GA;;;AC)"), ("aar", "(A;;GA;;;S-1-15-2-2)"), ("world", "")):
            outside = self.outside_fixture(name + ".txt", grant)
            before = native.security_fingerprint(outside)
            for forced in (False, True):
                with self.subTest(grant=name, forced=forced):
                    session = self.session()
                    receipt = session.temp / "child.txt"
                    args = ["--child-parent", str(receipt), str(outside)]
                    session.launch(args + (["--off"] if forced else []))
                    session.stdin.close()
                    self.assertEqual(session.wait(10), 0)
                    text = receipt.read_text()
                    for expected in ("genuine_appcontainer=1", "win32k=1", "outside_write=0", "allowed_write=1", "restricting_count=1", "restricting_sid=" + session._restriction_text):
                        self.assertIn(expected, text)
                    self.assertEqual(outside.read_text(), "UNCHANGED_OUTSIDE")
                    self.assertEqual(native.security_fingerprint(outside), before)

    def test_native_console_child_startup_failure_is_not_hidden(self):
        # Compatibility negative, NOT a successful child security gate.
        for mode in ("--ordinary", "--console"):
            session = self.session()
            receipt = session.temp / "unsupported-child.txt"
            session.launch(["--child-parent", str(receipt), str(self.root / "outside.txt"), mode])
            session.stdin.close()
            self.assertEqual(session.wait(10), 0xC0000142)
            self.assertFalse(receipt.exists())

    def test_native_job_cancel_kills_actual_child(self):
        from neoxider_agents.process import pid_alive, pid_stamp
        session = self.session()
        session.launch(["--sleep-parent"])
        session.stdin.close()
        child = None
        # Fixture emits five bounded lines immediately before sleeping.
        for unused in range(5):
            line = session.stdout.readline().decode().strip()
            if line.startswith("child_pid="):
                child = int(line.split("=", 1)[1])
                break
        self.assertIsNotNone(child)
        stamp = pid_stamp(child)
        self.assertTrue(pid_alive(child, stamp))
        session.terminate()
        self.assertIsNotNone(session.wait(5))
        deadline = time.monotonic() + 3
        while pid_alive(child, stamp) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(child, stamp))

    def test_native_desktop_station_and_parent_token_escape_guards(self):
        session = self.session()
        n, c, w = session._api, session._api.c, session._api.w
        get_sd = n.U.GetUserObjectSecurity
        get_sd.argtypes, get_sd.restype = [w.HANDLE,c.POINTER(w.DWORD),c.c_void_p,w.DWORD,c.POINTER(w.DWORD)], w.BOOL
        open_desktop = n.U.OpenDesktopW
        open_desktop.argtypes, open_desktop.restype = [w.LPCWSTR,w.DWORD,w.BOOL,w.DWORD], w.HANDLE
        desktop = n.check(open_desktop("Default",0,False,0x20000))
        station = n.GetProcessWindowStation()
        def fingerprint(handle):
            size, flags = w.DWORD(), w.DWORD(1|4)
            get_sd(handle,c.byref(flags),None,0,c.byref(size))
            data = c.create_string_buffer(size.value)
            n.check(get_sd(handle,c.byref(flags),data,len(data),c.byref(size)))
            return hashlib.sha256(data.raw[:size.value]).hexdigest()
        try:
            before = [fingerprint(h) for h in (desktop,station,session._desktop)]
            control = subprocess.run([str(self.exe), "--object-gate", str(os.getpid())],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(control.returncode, 0)
            self.assertIn(b"native_station_open=True", control.stdout)
            self.assertIn(b"native_desktop_open=True", control.stdout)
            session.launch(["--object-gate",str(os.getpid())])
            session.stdin.close()
            self.assertEqual(session.wait(10), 0)
            text = session.stdout.read().decode()
            for expected in ("win32k_enabled=True", "user32_loaded=False", "native_station_open=False", "native_desktop_open=False", "parent_mutation=False", "parent_token_duplicate=False"):
                self.assertIn(expected, text)
            self.assertEqual(before,[fingerprint(h) for h in (desktop,station,session._desktop)])
        finally:
            n.close_desktop(desktop)

    def test_native_local_pipe_namespace_isolation_with_healthy_host_controls(self):
        session = self.session()
        n, c, w = session._api, session._api.c, session._api.w
        create = n.K.CreateNamedPipeW
        create.argtypes, create.restype = [w.LPCWSTR,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,c.c_void_p], w.HANDLE
        disconnect = n.K.DisconnectNamedPipe
        disconnect.argtypes, disconnect.restype = [w.HANDLE], w.BOOL
        connect = n.K.ConnectNamedPipe
        connect.argtypes, connect.restype = [w.HANDLE,c.c_void_p], w.BOOL
        write = n.K.WriteFile
        write.argtypes, write.restype = [w.HANDLE,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p], w.BOOL
        path = r"\\.\pipe\LOCAL\neoxider-owned-" + uuid.uuid4().hex
        exact_path = path.replace(r"\\.\pipe", r"\\?\GLOBALROOT\Device\NamedPipe")
        sd = n.descriptor("D:P(A;;GA;;;WD)(A;;GA;;;AC)(A;;GA;;;S-1-15-2-2)S:(ML;;NW;;;LW)")
        sa = n.SA(c.sizeof(n.SA),sd,False)
        pipe = create(path,3|0x80000,1,1,4096,4096,0,c.byref(sa))  # Nonblocking server.
        n.LocalFree(sd)
        self.assertNotEqual(pipe,c.c_void_p(-1).value)
        def host_control(message):
            connected = connect(pipe,None)
            self.assertTrue(connected or c.get_last_error() in (535,536))
            client = n.CreateFileW(exact_path,0xc0000000,0,None,3,0,None)
            self.assertNotEqual(client,c.c_void_p(-1).value)
            try:
                size = w.DWORD()
                n.check(write(client,message,len(message),c.byref(size),None))
                buffer = c.create_string_buffer(128)
                n.check(n.ReadFile(pipe,buffer,len(buffer),c.byref(size),None))
                self.assertEqual(buffer.raw[:size.value],message)
            finally:
                n.close(client)
                n.check(disconnect(pipe))
        try:
            host_control(b"HOST_CONTROL_BEFORE")
            session.launch(["--pipe-gate",exact_path])
            session.stdin.close()
            self.assertEqual(session.wait(10),0)
            text = session.stdout.read().decode()
            self.assertIn("broker_open=False",text)
            # Windows returns NAME_NOT_FOUND in the AppContainer pipe namespace,
            # even for the GLOBALROOT spelling. This proves this owned broker
            # is inaccessible, not an ACL-denial or exhaustive broker guarantee.
            self.assertIn("broker_error=2",text)
            host_control(b"HOST_CONTROL_AFTER")
        finally:
            n.close(pipe)

    def test_native_loopback_broker_is_denied(self):
        server = socket.socket()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            def host_control(challenge):
                server.settimeout(1)
                with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
                    client.sendall(challenge)
                    peer, unused = server.accept()
                    with peer:
                        peer.settimeout(1)
                        self.assertEqual(peer.recv(128), challenge)
            host_control(b"HOST_CONTROL_BEFORE")
            session = self.session()
            session.launch([str(self.root / "outside.txt"), str(port)])
            session.stdin.write(b"NETWORK_DENIAL\n")
            session.stdin.close()
            self.assertEqual(session.wait(10), 0)
            text = session.stdout.read().decode()
            self.assertIn("genuine_appcontainer=True", text)
            self.assertIn("no_network_capabilities=True", text)
            self.assertIn("loopback_connected=False", text)
            # WFP may drop rather than return EACCES. A timeout ALONE is
            # inconclusive; the healthy same-listener controls and grace below
            # are required parts of this composite gate.
            self.assertTrue("loopback_error=10013" in text or "loopback_error=10060" in text)
            session.close()  # End attempted socket/process before late-traffic grace.
            server.settimeout(0.5)
            with self.assertRaises(socket.timeout):
                peer, unused = server.accept()
                peer.close()
            host_control(b"HOST_CONTROL_AFTER")
        finally:
            server.close()

    def test_bad_pin_and_requested_network_refuse_before_profile(self):
        with self.assertRaises(UnsafeScope):
            self.Session.prepare(self.request, {"fixture.exe": (str(self.exe), "0" * 64)},
                                 "fixture.exe", str(self.private_parent))
        with self.assertRaises(EnforcementUnavailable):
            self.Session.prepare(ScopeRequest(str(self.source), ("allowed.txt",), network=True),
                                 {"fixture.exe": (str(self.exe), self.pin)},
                                 "fixture.exe", str(self.private_parent))

    def test_native_hardlink_capture_refuses(self):
        from neoxider_agents.windows_enforcement import _Native
        alias = self.root / "alias.txt"
        os.link(str(self.source / "allowed.txt"), str(alias))
        with self.assertRaises(UnsafeScope):
            _Native().read_nofollow(alias)

    def test_native_runtime_tamper_refuses_before_launch(self):
        session = self.session()
        session.executable.write_bytes(b"tampered")
        with self.assertRaises(UnsafeScope) as caught:
            session.launch()
        self.assertEqual(caught.exception.code, "runtime-copy-changed")
        self.assertIsNone(session.pid)
        self.assertEqual(session.evidence["profile_delete_hresult"], 0)

    def test_staging_limits_and_disjoint_roots_refuse_before_profile(self):
        from neoxider_agents.windows_enforcement import ConsoleBudgets, _Native
        runtime = {"fixture.exe": (str(self.exe), self.pin)}
        with patch.object(_Native, "create_profile", side_effect=AssertionError("Profile created beyond staging budget")) as create:
            for budgets in (ConsoleBudgets(staging_bytes=8), ConsoleBudgets(staging_files=4)):
                with self.subTest(budgets=budgets), self.assertRaises(UnsafeScope):
                    self.Session.prepare(self.request, runtime, "fixture.exe", str(self.private_parent), budgets)
            nested = self.source / "nested-private"
            nested.mkdir()
            for parent in (nested, self.source, self.root):
                with self.subTest(parent=parent), self.assertRaises(UnsafeScope) as caught:
                    self.Session.prepare(self.request, runtime, "fixture.exe", str(parent))
                self.assertEqual(caught.exception.code, "overlapping-roots")
            create.assert_not_called()

    def test_directory_depth_time_and_runtime_directory_budgets(self):
        from neoxider_agents.windows_enforcement import ConsoleBudgets, _Native, _StageMeter
        runtime = {"a/b/c/fixture.exe": (str(self.exe), self.pin)}
        with patch.object(_Native, "create_profile", side_effect=AssertionError("Profile created after budget failure")):
            with self.assertRaises(UnsafeScope):
                self.Session.prepare(self.request, runtime, "a/b/c/fixture.exe", str(self.private_parent), ConsoleBudgets(staging_files=10))
            deep = self.source / "writable" / "a" / "b"
            deep.mkdir(parents=True)
            with self.assertRaises(UnsafeScope):
                self.Session.prepare(self.request, runtime, "a/b/c/fixture.exe", str(self.private_parent), ConsoleBudgets(staging_depth=1))
        meter = _StageMeter(ConsoleBudgets(staging_seconds=1), time.monotonic()-2, self.source)
        with self.assertRaises(UnsafeScope):
            meter.visit(self.source)
        with patch.object(_StageMeter, "check_time", side_effect=UnsafeScope("staging-budget", "injected deadline")):
            with self.assertRaises(UnsafeScope):
                self.Session.prepare(self.request, runtime, "a/b/c/fixture.exe", str(self.private_parent))

    def test_native_capture_deadline_closes_all_held_handles(self):
        from neoxider_agents.windows_enforcement import _Native
        native = _Native()
        target = self.root / "partial-copy.txt"
        with self.assertRaises(UnsafeScope):
            native.read_nofollow(self.source / "allowed.txt", destination=target,
                check_budget=lambda: (_ for _ in ()).throw(UnsafeScope("budget", "deadline")))
        # Windows rename succeeds only after the held share-read handles close.
        moved = self.root / "moved-source"
        self.source.rename(moved)
        moved.rename(self.source)
        target.unlink()

    def test_native_job_budgets_query_and_bounded_allocation_denial(self):
        from neoxider_agents.windows_enforcement import ConsoleBudgets
        budgets = ConsoleBudgets(active_processes=1,process_memory_bytes=32*1024**2,job_memory_bytes=48*1024**2)
        session = self.Session.prepare(self.request, {"fixture.exe":(str(self.exe),self.pin)},
            "fixture.exe",str(self.private_parent),budgets)
        self.sessions.append(session)
        session.launch(["--budget-gate"])
        session.stdin.close()
        self.assertEqual(session.wait(10), 0)
        text = session.stdout.read().decode()
        for expected in ("job_flags=8968", "process_limit=1", "process_memory=33554432", "job_memory=50331648", "bounded_allocation_denied=True", "over_limit_child=False"):
            self.assertIn(expected, text)

    def test_cleanup_fault_continues_and_keeps_failed_resource_for_retry(self):
        from neoxider_agents.enforcement import EnforcementError
        session = self.session()
        session.launch(["--sleep-child"])
        session.stdin.close()
        self.assertTrue(session.stdout.readline())
        real = session.stdout
        class BrokenStream:
            closed = False
            def close(self):
                raise OSError("injected stream close failure")
        broken = BrokenStream()
        session.stdout = broken
        with self.assertRaises(EnforcementError):
            session.close()
        self.assertIsNone(session._job)
        self.assertIsNone(session._process)
        self.assertIsNone(session._desktop)
        self.assertIsNone(session._profile)
        self.assertIsNone(session._sid)
        self.assertIsNone(session._restriction_sid)
        self.assertTrue(real.closed)
        self.assertIn(broken, session._owned_streams)
        self.assertEqual(session.evidence["cleanup_pending"], ["stream-close"])
        broken.close = lambda: setattr(broken, "closed", True)
        session.close()
        self.assertFalse(session.evidence["cleanup_pending"])
        self.assertEqual(len(session.evidence["cleanup_errors"]), 2)

    def test_cleanup_retries_failed_desktop_and_profile_independently(self):
        session = self.session()
        real_close = session._api.close_desktop
        with patch.object(session._api, "close_desktop") as desktop:
            with patch.object(session._api, "delete_profile", side_effect=[5, 5]):
                # Keep the real desktop alive until the second attempted close.
                desktop.side_effect = lambda handle: real_close(handle) if desktop.call_count > 1 else (_ for _ in ()).throw(OSError("injected desktop close"))
                from neoxider_agents.enforcement import EnforcementError
                with self.assertRaises(EnforcementError):
                    session.close()
        self.assertIsNone(session._desktop)
        self.assertIsNotNone(session._profile)
        self.assertIsNone(session._sid)
        session.close()
        self.assertEqual(session.evidence["profile_delete_hresult"], 0)

    def test_prepare_failure_preserves_original_error_and_retryable_cleanup_owner(self):
        from neoxider_agents.windows_enforcement import _Native
        with patch.object(_Native, "permissions", side_effect=UnsafeScope("injected-prepare", "preserve me")):
            with patch.object(_Native, "delete_profile", return_value=5):
                with self.assertRaises(UnsafeScope) as caught:
                    self.Session.prepare(self.request, {"fixture.exe":(str(self.exe),self.pin)},
                        "fixture.exe",str(self.private_parent))
        self.assertEqual(caught.exception.code,"injected-prepare")
        session = caught.exception.cleanup_session
        self.sessions.append(session)
        self.assertIsNotNone(session._profile)
        self.assertIn("profile-delete",caught.exception.cleanup_evidence["cleanup_pending"])
        session.close()
        self.assertEqual(session.evidence["profile_delete_hresult"],0)

    def test_profile_conversion_failure_registers_retryable_ownership(self):
        from neoxider_agents.windows_enforcement import _Native
        real_init = _Native.__init__
        def failing_init(self):
            real_init(self)
            delete = self.DeleteAppContainerProfile
            calls = {"delete": 0}
            def fail_convert(sid, text):
                try:
                    self.c.set_last_error(5)
                except AttributeError:
                    pass
                return 0
            def fail_twice_delete(name):
                calls["delete"] += 1
                if calls["delete"] <= 2:
                    return 5  # E_ACCESSDENIED on both bounded cleanup passes.
                return delete(name)
            self.ConvertSidToStringSidW = fail_convert
            self.DeleteAppContainerProfile = fail_twice_delete
            real_free = self.free_sid
            freed = []
            def record_free(sid):
                freed.append(sid)
                return real_free(sid)
            self.free_sid = record_free
            self._fault_freed = freed
        with patch.object(_Native, "__init__", failing_init):
            with self.assertRaises(OSError) as caught:
                self.Session.prepare(self.request,
                    {"fixture.exe": (str(self.exe), self.pin)},
                    "fixture.exe", str(self.private_parent))
        error = caught.exception
        # The original conversion failure stays authoritative, not the delete fault.
        self.assertIsInstance(error, OSError)
        self.assertNotEqual(getattr(error, "code", None), "cleanup-failed")
        session = error.cleanup_session
        self.sessions.append(session)
        owned_name = session._profile
        # A real OS profile was created and registered before conversion: the
        # owned name is exact, never foreign or uncreated.
        self.assertEqual(owned_name, session.name)
        # The real SID was registered on creation, then released through the
        # checked path during cleanup: exactly one checked free ran.
        self.assertEqual(len(session._api._fault_freed), 1)
        self.assertIsNone(session._sid)
        self.assertIn("profile-delete", error.cleanup_evidence["cleanup_pending"])
        # The uniquely owned profile still exists while deletion is pending.
        check = _Native()
        fresh = check.c.c_void_p()
        code = check.CreateAppContainerProfile(owned_name, owned_name,
            "Owned Neoxider console session", None, 0, check.c.byref(fresh))
        try:
            self.assertNotEqual(code, 0)
        finally:
            if code == 0:
                try:
                    check.free_sid(fresh)
                finally:
                    check.delete_profile(owned_name)
        # Explicit retry uses the checked path and really removes the profile.
        session.close()
        self.assertFalse(session.evidence["cleanup_pending"])
        self.assertIsNone(session._profile)
        self.assertEqual(session.evidence["profile_delete_hresult"], 0)
        verify = _Native()
        recreated = verify.c.c_void_p()
        try:
            self.assertEqual(verify.CreateAppContainerProfile(owned_name, owned_name,
                "Owned Neoxider console session", None, 0, verify.c.byref(recreated)), 0)
        finally:
            try:
                verify.free_sid(recreated)
            finally:
                self.assertEqual(verify.delete_profile(owned_name), 0)

    def test_wait_duplicate_close_failure_is_retained_for_retry(self):
        session = self.session()
        session.launch([str(self.root / "outside.txt")])
        session.stdin.write(b"NEOXIDER_NATIVE_STDIO\n")
        session.stdin.close()
        self.assertEqual(session.wait(10), 0)
        owned_process = session._process
        real_close = session._api.close
        duplicates, retried, calls = [], [], {"count": 0}
        def flaky_close(handle):
            # Fail only the next two wait-duplicate releases; the retry and
            # every other release use the real checked close.
            calls["count"] += 1
            if calls["count"] <= 2:
                duplicates.append(handle)
                raise OSError("injected wait duplicate close failure")
            retried.append(handle)
            return real_close(handle)
        session._api.close = flaky_close
        with self.assertRaises(OSError):
            session.wait(0)
        with self.assertRaises(OSError):
            session.wait(0)
        # Both failed duplicates are distinct owned handles, never the original.
        self.assertEqual(len(duplicates), 2)
        self.assertNotEqual(duplicates[0], duplicates[1])
        self.assertTrue(all(handle != owned_process for handle in duplicates))
        with session._api._release_lock:
            pending = [label for label, action in session._api._pending_releases]
        self.assertEqual(pending, ["wait-duplicate-close", "wait-duplicate-close"])
        self.assertTrue(all(step["step"] == "wait-duplicate-close"
                            for step in session._api.cleanup_errors))
        # Both retained duplicates are still valid owned process handles.
        for handle in duplicates:
            self.assertIsNotNone(session._api.poll(handle))
        session.close()
        # The retry released the exact retained duplicates; nothing is pending.
        self.assertTrue(all(handle in retried for handle in duplicates))
        self.assertFalse(session.evidence["cleanup_pending"])
        self.assertFalse(session.evidence["native_cleanup_pending"])
        self.assertFalse(session._api._pending_releases)
        self.assertEqual(session.evidence["profile_delete_hresult"], 0)

    def test_permanent_job_terminate_failure_still_closes_job_and_kills_child(self):
        from neoxider_agents.process import pid_alive, pid_stamp
        session = self.session()
        session.launch(["--sleep-parent"])
        # Captured creation/token/job controls from the real launch.
        self.assertEqual(session.evidence["launch_token"]["appcontainer"], 1)
        self.assertTrue(session.evidence["launch_token"]["restricted"])
        session.stdin.close()
        child = None
        # Fixture emits bounded lines immediately before sleeping.
        for unused in range(5):
            line = session.stdout.readline().decode().strip()
            if line.startswith("child_pid="):
                child = int(line.split("=", 1)[1])
                break
        self.assertIsNotNone(child)
        stamp = pid_stamp(child)
        self.assertTrue(pid_alive(child, stamp))
        job = session._job
        self.assertIsNotNone(job)
        attempts, closes = {"terminate": 0}, []
        real_close = session._api.close
        def permanent_terminate_failure(handle, code):
            attempts["terminate"] += 1
            raise OSError(5, "injected permanent job termination failure")
        def recording_close(handle):
            closes.append(handle)
            return real_close(handle)
        session._api.TerminateJobObject = permanent_terminate_failure
        session._api.close = recording_close
        # Termination fails permanently, but the independent job-close attempt
        # must still run: the error is preserved and the handle is released.
        with self.assertRaises(OSError) as caught:
            session.terminate()
        self.assertIn("injected permanent job termination failure", str(caught.exception))
        self.assertGreaterEqual(attempts["terminate"], 1)
        self.assertIn(job, closes)
        self.assertIsNone(session._job)
        # KILL_ON_JOB_CLOSE killed the benign detached child on job closure.
        deadline = time.monotonic() + 5
        while pid_alive(child, stamp) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(child, stamp))
        self.assertIsNotNone(session.wait(5))
        session.close()
        self.assertFalse(session.evidence["cleanup_pending"])
        self.assertEqual(session.evidence["profile_delete_hresult"], 0)

    def test_native_stdio_whitelist_excludes_real_inheritable_file(self):
        session = self.session()
        native, c, w = session._api, session._api.c, session._api.w
        sentinel = self.root / "inheritable-outside.txt"
        sa = native.SA(c.sizeof(native.SA), None, True)
        handle = native.CreateFileW(str(sentinel), 0x40000000, 7,
                                   c.byref(sa), 2, 0, None)
        self.assertNotEqual(handle, c.c_void_p(-1).value)
        copied = w.HANDLE()
        try:
            original = c.create_string_buffer(52)
            native.check(native.GetFileInformationByHandle(handle, original))
            session.launch(["--sleep-parent"])
            session.stdin.close()
            # Validate a genuinely running native process first.
            first = session.stdout.readline().decode()
            self.assertIn("no_network_capabilities=True", first)
            leaked = native.DuplicateHandle(session._process, handle,
                native.GetCurrentProcess(), c.byref(copied), 0, False, 2)
            if leaked:
                observed = c.create_string_buffer(52)
                query = native.GetFileInformationByHandle(copied, observed)
                if query:
                    # Numeric handle reuse is possible; compare file identity.
                    import struct
                    expected = struct.unpack("13I", original.raw)
                    actual = struct.unpack("13I", observed.raw)
                    self.assertNotEqual((expected[7], expected[11], expected[12]),
                                        (actual[7], actual[11], actual[12]))
            session.terminate()
            session.wait(5)
            self.assertEqual(sentinel.read_bytes(), b"")
        finally:
            if copied:
                native.close(copied)
            native.close(handle)

    def test_native_wait_and_close_keep_distinct_wait_handle(self):
        session = self.session()
        session.launch(["--sleep-child"])
        session.stdin.close()
        session.stdout.readline()
        entered = threading.Event()
        original_wait = session._api.wait
        results, errors = [], []
        def notified_wait(handle, timeout):
            entered.set()
            return original_wait(handle, timeout)
        session._api.wait = notified_wait
        def waiter():
            try:
                results.append(session.wait(10))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(entered.wait(2))
        session.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0])
        self.assertEqual(session.poll(), results[0])
        self.assertEqual(session.wait(0), results[0])

    def test_native_owner_dacl_and_cross_scope_mutation_is_denied(self):
        session = self.session()
        session.launch([str(self.root / "outside.txt"), "--acl", str(session.runtime / "guard.txt")])
        session.stdin.write(b"DACL_ESCAPE_ATTACK\n")
        session.stdin.close()
        self.assertEqual(session.wait(10), 0)
        text = session.stdout.read().decode()
        for label in ("protected", "runtime", "ancestor"):
            for action in ("dacl_change_allowed", "setfilesecurity_allowed", "owner_change_allowed"):
                self.assertIn(label + "_" + action + "=False", text)
        self.assertIn("protected_write_after_acl=False", text)
        self.assertIn("runtime_write_after_acl=False", text)
        self.assertIn("allowed_directory_write=True", text)
        self.assertIn("cross_scope_move_allowed=False", text)
        self.assertIn("runtime_rename_allowed=False", text)
        self.assertEqual((session.workspace / "protected.txt").read_text(), "original")
        self.assertEqual((session.runtime / "guard.txt").read_text(), "original")
        evidence_dir = os.environ.get("AGENT_ENFORCEMENT_EVIDENCE_DIR")
        if evidence_dir:
            path = Path(evidence_dir)
            self.assertTrue(path.is_absolute() and path.is_dir())
            session.close()
            (path / ("native-acl-" + uuid.uuid4().hex + ".json")).write_text(
                json.dumps({"native_stdout": text, "session": session.evidence,
                            "fixture_sha256": self.pin}, indent=2), encoding="utf-8")


@unittest.skipUnless(os.name == "nt", "Native profile-text ownership requires Windows.")
class ProfileTextReleaseTests(unittest.TestCase):
    def test_successful_conversion_releases_text_without_pending(self):
        from neoxider_agents.windows_enforcement import _Native
        from types import SimpleNamespace
        native = _Native()
        owner = SimpleNamespace()
        name = "neoxider-text-ok-" + uuid.uuid4().hex
        sid, text = native.create_profile(name, owner=owner)
        try:
            self.assertTrue(text.startswith("S-1-15-2-"))
            self.assertEqual(owner._profile, name)
            self.assertTrue(owner._sid)
            with native._release_lock:
                self.assertFalse(native._pending_releases)
            self.assertFalse([e for e in native.cleanup_errors
                              if e["step"] == "profile-sid-text-free"])
        finally:
            try:
                native.free_sid(sid)
            finally:
                self.assertEqual(native.delete_profile(name), 0)

    def test_successful_conversion_localfree_fault_retains_pointer_and_retries_once(self):
        from neoxider_agents.windows_enforcement import _Native
        from types import SimpleNamespace
        native = _Native()
        owner = SimpleNamespace()
        name = "neoxider-text-fault-" + uuid.uuid4().hex
        real_free = native.local_free
        failed = []
        def fail(pointer):
            failed.append(pointer.value)
            raise OSError(5, "injected profile text LocalFree fault")
        native.local_free = fail
        try:
            with self.assertRaises(OSError) as caught:
                native.create_profile(name, owner=owner)
        finally:
            native.local_free = real_free
        self.assertIn("injected profile text LocalFree fault", str(caught.exception))
        # Profile and SID were tracked immediately and stay owned for retry.
        self.assertEqual(owner._profile, name)
        self.assertTrue(owner._sid)
        self.assertEqual(len(failed), 1)
        with native._release_lock:
            pending = list(native._pending_releases)
        self.assertEqual([label for label, action in pending], ["profile-sid-text-free"])
        self.assertTrue(any(e["step"] == "profile-sid-text-free" for e in native.cleanup_errors))
        # Retry frees exactly the retained pointer, exactly once.
        freed = []
        real_retry = native.local_free
        def counting(pointer):
            freed.append(pointer.value)
            return real_retry(pointer)
        native.local_free = counting
        try:
            native.release_resources([])
        finally:
            native.local_free = real_retry
        self.assertEqual(freed, failed)
        with native._release_lock:
            self.assertFalse(native._pending_releases)
        # A second drain frees nothing: no double-free.
        native.release_resources([])
        self.assertEqual(len(freed), 1)
        try:
            native.free_sid(owner._sid)
        finally:
            self.assertEqual(native.delete_profile(name), 0)

    def test_conversion_failure_keeps_profile_and_sid_owned(self):
        from neoxider_agents.windows_enforcement import _Native
        from types import SimpleNamespace
        native = _Native()
        owner = SimpleNamespace()
        name = "neoxider-text-conv-" + uuid.uuid4().hex
        real_convert = native.ConvertSidToStringSidW
        def fail_convert(sid, text):
            try:
                native.c.set_last_error(5)
            except AttributeError:
                pass
            return 0
        native.ConvertSidToStringSidW = fail_convert
        try:
            with self.assertRaises(OSError):
                native.create_profile(name, owner=owner)
        finally:
            native.ConvertSidToStringSidW = real_convert
        self.assertEqual(owner._profile, name)
        self.assertTrue(owner._sid)
        try:
            fresh = native.c.c_void_p()
            self.assertNotEqual(native.CreateAppContainerProfile(
                name, name, "Owned Neoxider console session", None, 0,
                native.c.byref(fresh)), 0)
        finally:
            try:
                native.free_sid(owner._sid)
            finally:
                self.assertEqual(native.delete_profile(name), 0)

    def test_absent_owner_rejected_before_os_creation(self):
        from neoxider_agents.windows_enforcement import _Native
        from neoxider_agents.enforcement import EnforcementError
        from types import SimpleNamespace
        native = _Native()
        name = "neoxider-text-noowner-" + uuid.uuid4().hex
        with self.assertRaises(EnforcementError) as caught:
            native.create_profile(name)
        self.assertEqual(caught.exception.code, "profile-owner-required")
        # Nothing was created: the same name is still free for owned creation.
        owner = SimpleNamespace()
        sid, text = native.create_profile(name, owner=owner)
        try:
            self.assertTrue(text.startswith("S-1-15-2-"))
        finally:
            try:
                native.free_sid(sid)
            finally:
                self.assertEqual(native.delete_profile(name), 0)


_WINDOWS_FIXTURE_SOURCE = r"""#include <winsock2.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sddl.h>
#include <aclapi.h>
#include <winternl.h>
static void out(const char *key,int value){printf("%s=%s\n",key,value?"True":"False");fflush(stdout);}
static int restricted_identity(FILE *output){
    HANDLE token;DWORD bytes=0,i;TOKEN_GROUPS *groups;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token))return 0;
    GetTokenInformation(token,TokenRestrictedSids,NULL,0,&bytes);groups=HeapAlloc(GetProcessHeap(),0,bytes);
    if(!groups || !GetTokenInformation(token,TokenRestrictedSids,groups,bytes,&bytes)){if(groups)HeapFree(GetProcessHeap(),0,groups);CloseHandle(token);return 0;}
    fprintf(output,"restricting_count=%lu\n",groups->GroupCount);
    for(i=0;i<groups->GroupCount;i++){char *text=NULL;if(!ConvertSidToStringSidA(groups->Groups[i].Sid,&text))return 0;
        fprintf(output,"restricting_sid=%s\n",text);LocalFree(text);}
    HeapFree(GetProcessHeap(),0,groups);CloseHandle(token);return 1;
}
static int write(const char *path){HANDLE h=CreateFileA(path,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,0,NULL);DWORD n=0;if(h==INVALID_HANDLE_VALUE)return 0;WriteFile(h,"changed",7,&n,NULL);CloseHandle(h);return 1;}
static void acl_attack(const char *label,const char *path){
    HANDLE token;DWORD needed=0;void *package=NULL,*user=NULL;char *sidText=NULL;char sddl[1024],key[128];
    PSECURITY_DESCRIPTOR descriptor=NULL;PACL dacl=NULL;BOOL present=FALSE,defaulted=FALSE;DWORD error;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token))return;
    GetTokenInformation(token,TokenAppContainerSid,NULL,0,&needed);package=HeapAlloc(GetProcessHeap(),0,needed);
    if(!package || !GetTokenInformation(token,TokenAppContainerSid,package,needed,&needed))return;
    if(!ConvertSidToStringSidA(*(PSID*)package,&sidText))return;
    GetTokenInformation(token,TokenUser,NULL,0,&needed);user=HeapAlloc(GetProcessHeap(),0,needed);
    if(!user || !GetTokenInformation(token,TokenUser,user,needed,&needed))return;
    sprintf_s(sddl,sizeof(sddl),"D:P(A;;GA;;;OW)(A;;GA;;;%s)",sidText);
    if(!ConvertStringSecurityDescriptorToSecurityDescriptorA(sddl,1,&descriptor,NULL))return;
    if(!GetSecurityDescriptorDacl(descriptor,&present,&dacl,&defaulted))return;
    error=SetNamedSecurityInfoA((LPSTR)path,SE_FILE_OBJECT,DACL_SECURITY_INFORMATION|PROTECTED_DACL_SECURITY_INFORMATION,NULL,NULL,dacl,NULL);
    printf("%s_dacl_error=%lu\n",label,error);fflush(stdout);
    sprintf_s(key,sizeof(key),"%s_dacl_change_allowed",label);out(key,error==0);
    sprintf_s(key,sizeof(key),"%s_setfilesecurity_allowed",label);out(key,SetFileSecurityA(path,DACL_SECURITY_INFORMATION,descriptor));
    error=SetNamedSecurityInfoA((LPSTR)path,SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION,*(PSID*)user,NULL,NULL,NULL);
    printf("%s_owner_error=%lu\n",label,error);fflush(stdout);
    sprintf_s(key,sizeof(key),"%s_owner_change_allowed",label);out(key,error==0);
    sprintf_s(key,sizeof(key),"%s_write_after_acl",label);out(key,write(path));
    LocalFree(descriptor);LocalFree(sidText);HeapFree(GetProcessHeap(),0,package);HeapFree(GetProcessHeap(),0,user);CloseHandle(token);
}
int main(int argc,char **argv){
    HANDLE token; DWORD app=0,needed=0,policy=0;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token))return 11;
    if(!GetTokenInformation(token,TokenIsAppContainer,&app,sizeof(app),&needed))return 12;
    {DWORD bytes=0;void *caps=NULL;
        GetTokenInformation(token,TokenCapabilities,NULL,0,&bytes);
        caps=HeapAlloc(GetProcessHeap(),0,bytes);
        if(!caps || !GetTokenInformation(token,TokenCapabilities,caps,bytes,&needed))return 29;
        out("no_network_capabilities",*(DWORD*)caps==0);HeapFree(GetProcessHeap(),0,caps);}
    CloseHandle(token);
    out("genuine_appcontainer",app==1);
    out("mitigation_query",GetProcessMitigationPolicy(GetCurrentProcess(),ProcessSystemCallDisablePolicy,&policy,sizeof(policy)));
    out("win32k_enabled",(policy&1)!=0);
    if(argc>1 && strcmp(argv[1],"--object-gate")==0){
        HMODULE module=LoadLibraryExA("user32.dll",NULL,LOAD_LIBRARY_SEARCH_SYSTEM32);HANDLE parent;
        out("user32_loaded",module!=NULL);if(module)FreeLibrary(module);
        module=LoadLibraryExA("win32u.dll",NULL,LOAD_LIBRARY_SEARCH_SYSTEM32);
        out("win32u_loaded",module!=NULL);
        {typedef HANDLE (WINAPI *OpenStationFn)(OBJECT_ATTRIBUTES*,ACCESS_MASK);
         typedef HANDLE (WINAPI *OpenDesktopFn)(OBJECT_ATTRIBUTES*,DWORD,ACCESS_MASK);
         typedef BOOL (WINAPI *CloseObjectFn)(HANDLE);
         OpenStationFn openStation=module?(OpenStationFn)GetProcAddress(module,"NtUserOpenWindowStation"):NULL;
         OpenDesktopFn openDesktop=module?(OpenDesktopFn)GetProcAddress(module,"NtUserOpenDesktop"):NULL;
         CloseObjectFn closeStation=module?(CloseObjectFn)GetProcAddress(module,"NtUserCloseWindowStation"):NULL;
         CloseObjectFn closeDesktop=module?(CloseObjectFn)GetProcAddress(module,"NtUserCloseDesktop"):NULL;
         UNICODE_STRING name={0};OBJECT_ATTRIBUTES attributes={0};HANDLE station=NULL,desktop=NULL;
         WCHAR stationName[256];DWORD sessionId=0;ProcessIdToSessionId(GetCurrentProcessId(),&sessionId);
         swprintf_s(stationName,256,L"\\Sessions\\%lu\\Windows\\WindowStations\\WinSta0",sessionId);
         name.Buffer=stationName;name.Length=(USHORT)(wcslen(stationName)*2);name.MaximumLength=name.Length+2;
         attributes.Length=sizeof(attributes);attributes.ObjectName=&name;attributes.Attributes=0x40;
         if(!openStation || !openDesktop || !closeStation || !closeDesktop)return 33;
         __try {station=openStation(&attributes,WINSTA_CREATEDESKTOP|WINSTA_WRITEATTRIBUTES|WINSTA_ACCESSCLIPBOARD);}
         __except(EXCEPTION_EXECUTE_HANDLER){printf("native_station_exception=0x%lx\n",GetExceptionCode());}
         out("native_station_open",station!=NULL);
         wcscat_s(stationName,256,L"\\Default");name.Length=(USHORT)(wcslen(stationName)*2);name.MaximumLength=name.Length+2;
         __try {desktop=openDesktop(&attributes,0,DESKTOP_WRITEOBJECTS|DESKTOP_HOOKCONTROL|DESKTOP_SWITCHDESKTOP);}
         __except(EXCEPTION_EXECUTE_HANDLER){printf("native_desktop_exception=0x%lx\n",GetExceptionCode());}
         out("native_desktop_open",desktop!=NULL);if(desktop)closeDesktop(desktop);if(station)closeStation(station);}
        if(module)FreeLibrary(module);
        parent=OpenProcess(PROCESS_VM_WRITE|PROCESS_VM_OPERATION|PROCESS_DUP_HANDLE,FALSE,(DWORD)atoi(argv[2]));
        out("parent_mutation",parent!=NULL);if(parent)CloseHandle(parent);
        parent=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,FALSE,(DWORD)atoi(argv[2]));
        {BOOL duplicate=parent && OpenProcessToken(parent,TOKEN_DUPLICATE|TOKEN_IMPERSONATE|TOKEN_ASSIGN_PRIMARY,&token);
         out("parent_token_duplicate",duplicate);if(duplicate)CloseHandle(token);if(parent)CloseHandle(parent);}
        return 0;
    }
    if(argc>1 && strcmp(argv[1],"--pipe-gate")==0){
        HANDLE pipe=CreateFileA(argv[2],GENERIC_READ|GENERIC_WRITE,0,NULL,OPEN_EXISTING,0,NULL);
        DWORD error=pipe==INVALID_HANDLE_VALUE?GetLastError():0;
        out("broker_open",pipe!=INVALID_HANDLE_VALUE);printf("broker_error=%lu\n",error);
        if(pipe!=INVALID_HANDLE_VALUE)CloseHandle(pipe);return 0;
    }
    if(argc>1 && strcmp(argv[1],"--budget-gate")==0){
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits={0};void *memory;char self[MAX_PATH],cmd[2048];
        STARTUPINFOA si={sizeof(si)};PROCESS_INFORMATION pi={0};BOOL started;
        if(!QueryInformationJobObject(NULL,JobObjectExtendedLimitInformation,&limits,sizeof(limits),NULL))return 32;
        printf("job_flags=%lu\nprocess_limit=%lu\nprocess_memory=%llu\njob_memory=%llu\n",limits.BasicLimitInformation.LimitFlags,
            limits.BasicLimitInformation.ActiveProcessLimit,(unsigned long long)limits.ProcessMemoryLimit,(unsigned long long)limits.JobMemoryLimit);
        memory=VirtualAlloc(NULL,64*1024*1024,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
        out("bounded_allocation_denied",memory==NULL);if(memory)VirtualFree(memory,0,MEM_RELEASE);
        GetModuleFileNameA(NULL,self,sizeof(self));sprintf_s(cmd,sizeof(cmd),"\"%s\" --sleep-child",self);
        started=CreateProcessA(NULL,cmd,NULL,NULL,FALSE,DETACHED_PROCESS,NULL,NULL,&si,&pi);out("over_limit_child",started);
        if(started){TerminateProcess(pi.hProcess,125);WaitForSingleObject(pi.hProcess,1000);CloseHandle(pi.hThread);CloseHandle(pi.hProcess);}
        return 0;
    }
    if(argc>1 && strcmp(argv[1],"--child-gate")==0){
        FILE *f=NULL;if(argc!=4 || fopen_s(&f,argv[2],"w")!=0)return 21;
        fprintf(f,"genuine_appcontainer=%lu\nwin32k=%lu\noutside_write=%d\nallowed_write=%d\n",app,policy&1,write(argv[3]),write("allowed.txt"));
        if(!restricted_identity(f)){fclose(f);return 30;}fclose(f);return 0;
    }
    if(argc>1 && strcmp(argv[1],"--child-parent")==0){
        char self[MAX_PATH],cmd[2048];STARTUPINFOEXA sx={0};PROCESS_INFORMATION pi={0};SIZE_T size=0;
        DWORD64 off=PROCESS_CREATION_MITIGATION_POLICY_WIN32K_SYSTEM_CALL_DISABLE_ALWAYS_OFF;
        BOOL forced=argc==5 && strcmp(argv[4],"--off")==0;
        DWORD flags=argc==5 && strcmp(argv[4],"--ordinary")==0?0:argc==5 && strcmp(argv[4],"--console")==0?CREATE_NO_WINDOW:DETACHED_PROCESS;
        DWORD child_exit=0;
        GetModuleFileNameA(NULL,self,sizeof(self));sprintf_s(cmd,sizeof(cmd),"\"%s\" --child-gate \"%s\" \"%s\"",self,argv[2],argv[3]);
        sx.StartupInfo.cb=forced?sizeof(sx):sizeof(STARTUPINFOA);
        if(forced){InitializeProcThreadAttributeList(NULL,1,0,&size);sx.lpAttributeList=HeapAlloc(GetProcessHeap(),0,size);if(!sx.lpAttributeList)return 22;
        if(!InitializeProcThreadAttributeList(sx.lpAttributeList,1,0,&size))return 23;
        if(!UpdateProcThreadAttribute(sx.lpAttributeList,0,PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY,&off,sizeof(off),NULL,NULL))return 24;}
        if(!CreateProcessA(NULL,cmd,NULL,NULL,FALSE,flags|(forced?EXTENDED_STARTUPINFO_PRESENT:0),NULL,NULL,&sx.StartupInfo,&pi))return 25;
        if(WaitForSingleObject(pi.hProcess,5000)!=WAIT_OBJECT_0){TerminateProcess(pi.hProcess,125);return 26;}
        if(!GetExitCodeProcess(pi.hProcess,&child_exit))return 31;
        CloseHandle(pi.hThread);CloseHandle(pi.hProcess);
        if(forced){DeleteProcThreadAttributeList(sx.lpAttributeList);HeapFree(GetProcessHeap(),0,sx.lpAttributeList);}
        return (int)child_exit;
    }
    if(argc>1 && strcmp(argv[1],"--sleep-child")==0){Sleep(60000);return 0;}
    if(argc>1 && strcmp(argv[1],"--sleep-parent")==0){
        char self[MAX_PATH],cmd[2*MAX_PATH];STARTUPINFOA si={sizeof(si)};PROCESS_INFORMATION pi={0};
        GetModuleFileNameA(NULL,self,sizeof(self));sprintf_s(cmd,sizeof(cmd),"\"%s\" --sleep-child",self);
        if(!CreateProcessA(NULL,cmd,NULL,NULL,FALSE,DETACHED_PROCESS,NULL,NULL,&si,&pi))return 20;
        printf("child_pid=%lu\n",pi.dwProcessId);fflush(stdout);CloseHandle(pi.hThread);CloseHandle(pi.hProcess);Sleep(60000);return 0;
    }
    {char input[256]={0};DWORD read=0;if(!ReadFile(GetStdHandle(STD_INPUT_HANDLE),input,sizeof(input)-1,&read,NULL))return 13;
    printf("stdin_echo=%s",input);fflush(stdout);}
    out("allowed_write",write("allowed.txt"));
    out("protected_write",write("protected.txt"));
    if(argc>1)out("outside_write",write(argv[1]));
    if(argc>3 && strcmp(argv[2],"--acl")==0){
        out("allowed_directory_write",write("writable/allowed-dir.txt"));
        acl_attack("protected","protected.txt");acl_attack("runtime",argv[3]);acl_attack("ancestor",".");
        out("cross_scope_move_allowed",MoveFileA("protected.txt","writable/moved.txt"));
        out("runtime_rename_allowed",MoveFileA(argv[3],"writable/runtime-moved.txt"));
    }
    else if(argc>2){WSADATA ws;SOCKET s;struct sockaddr_in address={0};int connected,error;u_long nonblock=1;
        printf("network_before_startup=True\n");fflush(stdout);
        if(WSAStartup(MAKEWORD(2,2),&ws)!=0)return 27;
        printf("network_startup=True\n");fflush(stdout);
        s=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);printf("network_socket=%llu\n",(unsigned long long)s);fflush(stdout);
        if(s==INVALID_SOCKET){printf("loopback_error=%d\n",WSAGetLastError());out("loopback_connected",0);return 0;}
        if(ioctlsocket(s,FIONBIO,&nonblock)!=0)return 28;
        address.sin_family=AF_INET;address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);address.sin_port=htons((unsigned short)atoi(argv[2]));
        connected=connect(s,(struct sockaddr*)&address,sizeof(address));error=connected?WSAGetLastError():0;
        if(connected && error==WSAEWOULDBLOCK){fd_set ready,failed;struct timeval timeout={2,0};int wait;
        FD_ZERO(&ready);FD_ZERO(&failed);FD_SET(s,&ready);FD_SET(s,&failed);wait=select(0,NULL,&ready,&failed,&timeout);
        if(wait>0){int length=sizeof(error);getsockopt(s,SOL_SOCKET,SO_ERROR,(char*)&error,&length);connected=error?SOCKET_ERROR:0;}
        else{error=wait==0?WSAETIMEDOUT:WSAGetLastError();connected=SOCKET_ERROR;}}
        out("loopback_connected",connected==0);printf("loopback_error=%d\n",error);closesocket(s);WSACleanup();}
    return 0;
}
"""

def build_windows_fixture(output):
    """Compile the auditable native test source using an already-installed MSVC."""
    import sys
    output = Path(output)
    if os.name != "nt" or not output.is_absolute() or not output.parent.is_dir():
        raise RuntimeError("Windows and an explicit existing absolute output directory required.")
    source, obj, script = (output.with_suffix(s) for s in (".c", ".obj", ".cmd"))
    if any(path.exists() for path in (output, source, obj, script)):
        raise RuntimeError("Fixture builder refuses to overwrite existing artifacts.")
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    found = subprocess.run([str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    install = Path(found.stdout.decode("utf-8", errors="strict").strip())
    vcvars = install / "VC/Auxiliary/Build/vcvars64.bat"
    if not vcvars.is_file():
        raise RuntimeError("Installed MSVC native compiler unavailable; no install/fallback performed.")
    source.write_text(_WINDOWS_FIXTURE_SOURCE, encoding="ascii")
    script.write_text('@echo off\ncall "' + str(vcvars) + '" >nul\nif errorlevel 1 exit /b %errorlevel%\n'
        + 'cl.exe /nologo /W4 /MT /O1 /Fe:"' + str(output) + '" /Fo:"' + str(obj) + '" "' + str(source)
        + '" advapi32.lib ws2_32.lib\nexit /b %errorlevel%\n', encoding="ascii")
    built = subprocess.run(["cmd.exe", "/d", "/c", str(script)], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    if built.returncode:
        raise RuntimeError(built.stdout.decode(errors="replace"))
    return {"executable": str(output), "exe_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "compiler_output": built.stdout.decode(errors="replace")}


if __name__ == "__main__":
    unittest.main()
