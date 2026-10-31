#!/usr/bin/env python3
"""Stage an immutable Cloud Run release after a single-task migration job.

Run from a checkout in Google Cloud Shell. Inputs contain references to secrets,
never their values. Initial deployment stays IAM-private. Existing services retain
old traffic until the operator promotes the returned revision.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

REQUIRED = (
    "project_id",
    "region",
    "service_name",
    "image_uri",
    "instance_connection_name",
    "app_service_account",
    "migration_service_account",
    "db_name",
    "app_db_user",
    "migration_db_user",
    "django_secret",
    "app_db_secret",
    "migration_db_secret",
    "smtp_secret",
    "email_host",
    "email_port",
    "email_user",
    "from_email",
)


def validate(config):
    missing = [key for key in REQUIRED if not config.get(key)]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", config["project_id"]):
        raise ValueError("Invalid Google Cloud project ID.")
    for key in ["django_secret", "app_db_secret", "migration_db_secret", "smtp_secret"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+:[1-9][0-9]*", config[key]):
            raise ValueError(
                f"{key} must reference a secret and a numeric version, for example secret-name:1."
            )
    prefix = f'{config["region"]}-docker.pkg.dev/{config["project_id"]}/'
    if not config["image_uri"].startswith(prefix) or not re.search(
        r"@sha256:[a-f0-9]{64}$", config["image_uri"]
    ):
        raise ValueError(
            "Use an immutable Artifact Registry image digest in the same project and region."
        )
    if not config["instance_connection_name"].startswith(
        f'{config["project_id"]}:{config["region"]}:'
    ):
        raise ValueError("Use a Cloud SQL instance in the selected project and region.")
    if (
        config["app_db_user"] == config["migration_db_user"]
        or config["app_service_account"] == config["migration_service_account"]
    ):
        raise ValueError("Use separate application and migration identities.")
    for key in ["app_service_account", "migration_service_account"]:
        if not re.fullmatch(
            r"[a-z0-9-]+@"
            + re.escape(config["project_id"])
            + r"\.iam\.gserviceaccount\.com",
            config[key],
        ):
            raise ValueError(f"{key} must belong to the selected project.")
    if config.get("base_url"):
        parsed = urlsplit(config["base_url"])
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("base_url must be an HTTPS origin without a path.")
    if config.get("trusted_proxy_hops", "0") not in ["0", "1", "2", "3", "4", "5"]:
        raise ValueError(
            "trusted_proxy_hops must be 0 to 5 after verifying the ingress chain."
        )
    forbidden = {
        "DB_PASSWORD",
        "DJANGO_SECRET_KEY",
        "EMAIL_HOST_PASSWORD",
        "private_key",
        "password",
    }
    if forbidden.intersection(config):
        raise ValueError("Do not put secret values in the deployment configuration.")


def environment(config, base_url, migration=False):
    result = {
        "DJANGO_DEBUG": "0",
        "MANDALI_BASE_URL": base_url,
        "CLOUD_SQL_CONNECTION_NAME": config["instance_connection_name"],
        "DB_NAME": config["db_name"],
        "DB_USER": config["migration_db_user" if migration else "app_db_user"],
        "DB_CONN_MAX_AGE": "0" if migration else "60",
    }
    if not migration:
        result.update(
            {
                "DJANGO_TRUST_CLOUD_RUN_PROXY": "1",
                "DJANGO_TRUSTED_PROXY_HOPS": config.get("trusted_proxy_hops", "0"),
                "EMAIL_HOST": config["email_host"],
                "EMAIL_PORT": str(config["email_port"]),
                "EMAIL_HOST_USER": config["email_user"],
                "EMAIL_USE_TLS": "1",
                "DEFAULT_FROM_EMAIL": config["from_email"],
            }
        )
    return result


def secret_bindings(config, migration=False):
    values = {
        "DJANGO_SECRET_KEY": config["django_secret"],
        "DB_PASSWORD": config["migration_db_secret" if migration else "app_db_secret"],
    }
    if not migration:
        values["EMAIL_HOST_PASSWORD"] = config["smtp_secret"]
    return ",".join(f"{key}={value}" for key, value in values.items())


def gcloud(*args, capture=False, optional=False):
    result = subprocess.run(["gcloud", *args], text=True, capture_output=capture)
    if result.returncode and not optional:
        raise RuntimeError("gcloud command failed; release stopped before promotion.")
    return result


def stage(config):
    validate(config)
    common = [
        "--project",
        config["project_id"],
        "--region",
        config["region"],
        "--quiet",
    ]
    # Distinguish a missing service from a permission or connection failure.
    listing = gcloud("run", "services", "list", *common, "--format=json", capture=True)
    services = json.loads(listing.stdout)
    existing = next(
        (
            item
            for item in services
            if item.get("metadata", {}).get("name") == config["service_name"]
        ),
        None,
    )
    service_url = existing["status"]["url"] if existing else ""
    base_url = (
        config.get("base_url", "").rstrip("/")
        or service_url
        or "https://bootstrap.invalid"
    )
    with tempfile.TemporaryDirectory(prefix="mandali-release-") as temporary:
        runtime_file = Path(temporary) / "runtime.json"
        migration_file = Path(temporary) / "migration.json"
        runtime_file.write_text(json.dumps(environment(config, base_url)))
        migration_file.write_text(
            json.dumps(environment(config, base_url, migration=True))
        )
        job = config["service_name"] + "-migrate"
        gcloud(
            "run",
            "jobs",
            "deploy",
            job,
            *common,
            "--image",
            config["image_uri"],
            "--service-account",
            config["migration_service_account"],
            "--set-cloudsql-instances",
            config["instance_connection_name"],
            "--env-vars-file",
            str(migration_file),
            "--set-secrets",
            secret_bindings(config, migration=True),
            "--command",
            "python",
            "--args",
            "manage.py,migrate,--noinput",
            "--tasks",
            "1",
            "--parallelism",
            "1",
            "--max-retries",
            "0",
            "--task-timeout",
            "600s",
            "--cpu",
            "1",
            "--memory",
            "512Mi",
        )
        gcloud("run", "jobs", "execute", job, *common, "--wait")
        deployment = [
            "run",
            "deploy",
            config["service_name"],
            *common,
            "--image",
            config["image_uri"],
            "--service-account",
            config["app_service_account"],
            "--execution-environment",
            "gen2",
            "--port",
            "8080",
            "--add-cloudsql-instances",
            config["instance_connection_name"],
            "--env-vars-file",
            str(runtime_file),
            "--set-secrets",
            secret_bindings(config),
            "--min-instances",
            "0",
            "--max-instances",
            "5",
            "--concurrency",
            "16",
            "--cpu",
            "1",
            "--memory",
            "512Mi",
            "--timeout",
            "60s",
        ]
        deployment += (
            ["--no-traffic", "--tag", "candidate"]
            if existing
            else ["--no-allow-unauthenticated"]
        )
        gcloud(*deployment)
        description = gcloud(
            "run",
            "services",
            "describe",
            config["service_name"],
            *common,
            "--format=json",
            capture=True,
        )
        service = json.loads(description.stdout)
        if not existing and not config.get("base_url"):
            base_url = service["status"]["url"]
        candidate_url = (
            next(
                (
                    item.get("url", "")
                    for item in service["status"].get("traffic", [])
                    if item.get("tag") == "candidate"
                ),
                "",
            )
            if existing
            else ""
        )
        runtime = environment(config, base_url)
        origins = list(
            dict.fromkeys(
                origin
                for origin in [base_url, service["status"]["url"], candidate_url]
                if origin
            )
        )
        runtime["DJANGO_ALLOWED_HOSTS"] = ",".join(
            urlsplit(origin).hostname for origin in origins
        )
        runtime["DJANGO_CSRF_TRUSTED_ORIGINS"] = ",".join(origins)
        runtime_file.write_text(json.dumps(runtime))
        update = [
            "run",
            "services",
            "update",
            config["service_name"],
            *common,
            "--env-vars-file",
            str(runtime_file),
            "--set-secrets",
            secret_bindings(config),
        ]
        if existing:
            update += ["--no-traffic", "--tag", "candidate"]
        gcloud(*update)
        service = json.loads(
            gcloud(
                "run",
                "services",
                "describe",
                config["service_name"],
                *common,
                "--format=json",
                capture=True,
            ).stdout
        )
        revision = service["status"]["latestReadyRevisionName"]
        print(
            json.dumps(
                {
                    "service_url": service["status"]["url"],
                    "canonical_url": base_url,
                    "revision": revision,
                    "candidate_url": candidate_url,
                    "initial_deployment_private": not bool(existing),
                    "note": "Verify the release before publishing or changing traffic. See docs/GOOGLE_CLOUD.md.",
                },
                indent=2,
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        type=Path,
        help="Path to a local public configuration JSON; contains only secret references.",
    )
    args = parser.parse_args()
    try:
        stage(json.loads(args.config.read_text()))
    except (ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
