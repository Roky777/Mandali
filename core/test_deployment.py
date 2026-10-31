from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from config.environment import allowed_hosts, canonical_url, database_config
from core.security import client_ip
from scripts.deploy_google_cloud import environment, secret_bindings, stage, validate


class EnvironmentTests(SimpleTestCase):
    def test_local_sqlite_and_postgres_connection(self):
        self.assertEqual(
            database_config({}, True, Path("/tmp"))["ENGINE"],
            "django.db.backends.sqlite3",
        )
        config = database_config(
            {
                "DB_HOST": "127.0.0.1",
                "DB_NAME": "test",
                "DB_USER": "tester",
                "DB_PASSWORD": "dummy",
            },
            True,
            Path("/tmp"),
        )
        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertTrue(config["CONN_HEALTH_CHECKS"])

    def test_cloudsql_uses_proxy_socket_and_requires_credentials(self):
        env = {
            "CLOUD_SQL_CONNECTION_NAME": "example-project:asia-south1:mandali-db",
            "DB_NAME": "mandali",
            "DB_USER": "app",
            "DB_PASSWORD": "dummy",
        }
        config = database_config(env, False, Path("/tmp"))
        self.assertEqual(
            config["HOST"], "/cloudsql/example-project:asia-south1:mandali-db"
        )
        self.assertEqual(config["OPTIONS"]["sslmode"], "disable")
        for key in ["DB_NAME", "DB_USER", "DB_PASSWORD"]:
            with self.assertRaisesMessage(ImproperlyConfigured, key):
                database_config(
                    {k: v for k, v in env.items() if k != key}, False, Path("/tmp")
                )

    def test_production_never_falls_back_to_sqlite(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured, "Production requires PostgreSQL"
        ):
            database_config({}, False, Path("/tmp"))

    def test_ambiguous_or_insecure_database_configuration_fails(self):
        env = {"DB_NAME": "test", "DB_USER": "tester", "DB_PASSWORD": "dummy"}
        with self.assertRaises(ImproperlyConfigured):
            database_config({**env, "DB_HOST": "/tmp/untrusted"}, False, Path("/tmp"))
        with self.assertRaises(ImproperlyConfigured):
            database_config(
                {**env, "DB_HOST": "db.example.com", "DB_SSLMODE": "disable"},
                False,
                Path("/tmp"),
            )
        with self.assertRaises(ImproperlyConfigured):
            database_config(
                {
                    **env,
                    "DB_HOST": "localhost",
                    "CLOUD_SQL_CONNECTION_NAME": "example-project:asia-south1:db",
                },
                False,
                Path("/tmp"),
            )

    def test_canonical_origin_and_host_validation(self):
        self.assertEqual(
            canonical_url({"MANDALI_BASE_URL": "https://mandali.run.app/"}, False),
            "https://mandali.run.app",
        )
        self.assertEqual(
            allowed_hosts({}, False, "https://mandali.run.app"), ["mandali.run.app"]
        )
        for url in [
            "http://mandali.run.app",
            "https://user:pass@mandali.run.app",
            "https://mandali.run.app/path",
            "https://mandali.run.app?x=1",
            "",
        ]:
            with self.assertRaises(ImproperlyConfigured):
                canonical_url({"MANDALI_BASE_URL": url}, False)
        for host in ["*", "https://mandali.run.app", "mandali.run.app/path"]:
            with self.assertRaises(ImproperlyConfigured):
                allowed_hosts(
                    {"DJANGO_ALLOWED_HOSTS": host}, False, "https://mandali.run.app"
                )

    @override_settings(TRUSTED_PROXY_HOPS=0)
    def test_forwarded_ip_ignored_by_default(self):
        request = RequestFactory().get(
            "/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.9"
        )
        self.assertEqual(client_ip(request), "127.0.0.1")

    @override_settings(TRUSTED_PROXY_HOPS=1)
    def test_trusted_proxy_uses_rightmost_hop_not_spoofed_prefix(self):
        request = RequestFactory().get(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="attacker-supplied, 203.0.113.9",
        )
        self.assertEqual(client_ip(request), "203.0.113.9")
        request.META["HTTP_X_FORWARDED_FOR"] = "invalid"
        self.assertEqual(client_ip(request), "127.0.0.1")


class HealthTests(TestCase):
    def test_liveness_and_database_readiness(self):
        self.assertEqual(
            self.client.get(reverse("health_live")).json(), {"status": "ok"}
        )
        self.assertEqual(
            self.client.get(reverse("health_ready")).json(), {"status": "ok"}
        )

    def test_database_failure_is_generic_and_liveness_survives(self):
        with patch(
            "core.health.connection.cursor",
            side_effect=OperationalError("sensitive database details"),
        ):
            response = self.client.get(reverse("health_ready"))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"status": "unavailable"})
            self.assertNotIn("sensitive", response.content.decode())
            self.assertEqual(self.client.get(reverse("health_live")).status_code, 200)


class ReleaseTests(SimpleTestCase):
    def config(self):
        import json

        config = json.loads(Path("deploy/google-cloud/config.example.json").read_text())
        config["image_uri"] = (
            "asia-south1-docker.pkg.dev/your-project-id/mandali/web@sha256:" + "a" * 64
        )
        return config

    def test_release_uses_separate_identities_and_pinned_secrets(self):
        config = self.config()
        validate(config)
        runtime = environment(config, "https://mandali.run.app")
        migration = environment(config, "https://mandali.run.app", migration=True)
        self.assertNotEqual(runtime["DB_USER"], migration["DB_USER"])
        self.assertNotIn("DJANGO_TRUST_CLOUD_RUN_PROXY", migration)
        self.assertNotIn("DB_PASSWORD", runtime)
        self.assertIn("mandali-app-db-password:1", secret_bindings(config))
        self.assertIn(
            "mandali-migration-db-password:1", secret_bindings(config, migration=True)
        )
        self.assertNotIn("smtp", secret_bindings(config, migration=True))

    def test_mutable_images_secrets_and_shared_identities_are_rejected(self):
        for key, value in [
            ("image_uri", "image:latest"),
            ("django_secret", "secret:latest"),
            ("app_service_account", self.config()["migration_service_account"]),
            ("app_db_user", self.config()["migration_db_user"]),
            ("DB_PASSWORD", "do-not-store-secrets"),
        ]:
            config = {**self.config(), key: value}
            with self.assertRaises(ValueError):
                validate(config)

    def test_checkout_encryption_secret_is_pinned_and_runtime_only(self):
        config = {**self.config(), "payment_encryption_secret": "checkout-key:2"}
        validate(config)
        self.assertIn("PAYMENT_ENCRYPTION_KEYS=checkout-key:2", secret_bindings(config))
        self.assertNotIn("checkout-key", secret_bindings(config, migration=True))
        with self.assertRaises(ValueError):
            validate({**config, "payment_encryption_secret": "checkout-key:latest"})

    def test_migration_failure_stops_before_web_deployment(self):
        import subprocess

        def fake(*args, **kwargs):
            if args[:3] == ("run", "services", "list"):
                return subprocess.CompletedProcess([], 0, "[]", "")
            if args[:3] == ("run", "jobs", "execute"):
                raise RuntimeError("migration failed")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("scripts.deploy_google_cloud.gcloud", side_effect=fake) as calls:
            with self.assertRaisesMessage(RuntimeError, "migration failed"):
                stage(self.config())
        self.assertFalse(
            any(call.args[:2] == ("run", "deploy") for call in calls.call_args_list)
        )

    def test_first_release_stays_private_and_finalizes_canonical_url(self):
        import contextlib
        import io
        import json
        import subprocess

        environments = []
        service = {
            "metadata": {"name": "mandali"},
            "status": {
                "url": "https://mandali.run.app",
                "latestReadyRevisionName": "mandali-00002",
            },
        }

        def fake(*args, **kwargs):
            if "--env-vars-file" in args:
                environments.append(
                    json.loads(
                        Path(args[args.index("--env-vars-file") + 1]).read_text()
                    )
                )
            output = (
                "[]" if args[:3] == ("run", "services", "list") else json.dumps(service)
            )
            return subprocess.CompletedProcess([], 0, output, "")

        with patch(
            "scripts.deploy_google_cloud.gcloud", side_effect=fake
        ) as calls, contextlib.redirect_stdout(io.StringIO()):
            stage(self.config())
        deployment = next(
            call.args
            for call in calls.call_args_list
            if call.args[:2] == ("run", "deploy")
        )
        self.assertIn("--no-allow-unauthenticated", deployment)
        self.assertEqual(
            environments[-1]["MANDALI_BASE_URL"], "https://mandali.run.app"
        )
        self.assertEqual(environments[-1]["DJANGO_ALLOWED_HOSTS"], "mandali.run.app")

    def test_existing_release_preserves_traffic_and_allows_candidate_forms(self):
        import contextlib
        import io
        import json
        import subprocess

        environments = []
        service = {
            "metadata": {"name": "mandali"},
            "status": {
                "url": "https://mandali.run.app",
                "latestReadyRevisionName": "mandali-00003",
                "traffic": [
                    {"tag": "candidate", "url": "https://candidate---mandali.run.app"}
                ],
            },
        }

        def fake(*args, **kwargs):
            if "--env-vars-file" in args:
                environments.append(
                    json.loads(
                        Path(args[args.index("--env-vars-file") + 1]).read_text()
                    )
                )
            output = json.dumps(
                [service] if args[:3] == ("run", "services", "list") else service
            )
            return subprocess.CompletedProcess([], 0, output, "")

        with patch(
            "scripts.deploy_google_cloud.gcloud", side_effect=fake
        ) as calls, contextlib.redirect_stdout(io.StringIO()):
            stage(self.config())
        mutations = [
            call.args
            for call in calls.call_args_list
            if call.args[:2] == ("run", "deploy")
            or call.args[:3] == ("run", "services", "update")
        ]
        self.assertEqual(len(mutations), 2)
        self.assertTrue(all("--no-traffic" in command for command in mutations))
        self.assertIn(
            "candidate---mandali.run.app", environments[-1]["DJANGO_ALLOWED_HOSTS"]
        )
        self.assertIn(
            "https://candidate---mandali.run.app",
            environments[-1]["DJANGO_CSRF_TRUSTED_ORIGINS"],
        )


class ImageContentsTests(SimpleTestCase):
    def test_every_project_app_is_copied_into_the_image(self):
        from django.conf import settings

        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text()
        ignore = (root / ".dockerignore").read_text().split()
        for app in settings.INSTALLED_APPS:
            if (root / app).is_dir():
                self.assertIn(f"COPY {app} ./{app}", dockerfile, app)
                self.assertIn(f"!{app}/**", ignore, app)
