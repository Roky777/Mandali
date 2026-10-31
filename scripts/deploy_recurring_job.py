#!/usr/bin/env python3
"""Deploy the billing worker, without executing it or creating a scheduler."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.deploy_google_cloud import validate, environment, secret_bindings, gcloud


def stage_job(config):
    validate(config)
    if not config.get("base_url"):
        raise ValueError(
            "Set base_url to the deployed HTTPS origin before configuring billing jobs."
        )
    env = environment(config, config["base_url"])
    env.pop("DJANGO_TRUST_CLOUD_RUN_PROXY", None)
    env.pop("DJANGO_TRUSTED_PROXY_HOPS", None)
    env["DB_CONN_MAX_AGE"] = "0"
    with tempfile.TemporaryDirectory(prefix="mandali-billing-job-") as temporary:
        path = Path(temporary) / "environment.json"
        path.write_text(json.dumps(env))
        gcloud(
            "run",
            "jobs",
            "deploy",
            config["service_name"] + "-billing",
            "--project",
            config["project_id"],
            "--region",
            config["region"],
            "--image",
            config["image_uri"],
            "--service-account",
            config["app_service_account"],
            "--set-cloudsql-instances",
            config["instance_connection_name"],
            "--env-vars-file",
            str(path),
            "--set-secrets",
            secret_bindings(config),
            "--command",
            "python",
            "--args",
            "manage.py,run_recurring,--limit,100",
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
            "--quiet",
        )
    print(
        "Billing job deployed. It has not been executed. Configure the authenticated Cloud Scheduler trigger after verification."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    stage_job(json.loads(args.config.read_text()))
