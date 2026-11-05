"""Create a private pilot backup and verify restoration in an isolated database.

Run on the Compose host after stopping writers. No credentials or CV content are printed.
The output manifest is consumed by reset_pilot. Files contain sensitive data (mode 0600).
"""

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(["docker", "compose", *args], check=True, **kwargs)


def fingerprint(database, admin_id):
    # Fingerprint identity, MFA and permissions without exposing password hashes or OTP keys.
    query = f"""SELECT md5(concat(
      (SELECT row_to_json(u)::text FROM auth_user u WHERE id={admin_id}),
      (SELECT coalesce(json_agg(t ORDER BY id)::text,'[]') FROM otp_totp_totpdevice t
       WHERE user_id={admin_id}),
      (SELECT coalesce(json_agg(t ORDER BY id)::text,'[]') FROM otp_static_staticdevice t
       WHERE user_id={admin_id}),
      (SELECT coalesce(json_agg(t ORDER BY id)::text,'[]') FROM otp_static_statictoken t
       WHERE device_id IN (SELECT id FROM otp_static_staticdevice WHERE user_id={admin_id})),
      (SELECT coalesce(json_agg(t ORDER BY id)::text,'[]') FROM auth_user_groups t
       WHERE user_id={admin_id}),
      (SELECT coalesce(json_agg(t ORDER BY id)::text,'[]') FROM auth_user_user_permissions t
       WHERE user_id={admin_id})))"""
    return (
        run(
            ["exec", "-T", "db", "psql", "-U", "jobstream", "-d", database, "-Atc", query],
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-id", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.directory.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    dump = root / "database.dump"
    with dump.open("wb") as stream:
        run(
            ["exec", "-T", "db", "pg_dump", "-U", "jobstream", "-d", "jobstream", "-Fc"],
            stdout=stream,
        )
    # Copy referenced storage objects through Django's configured private storage backend.
    script = """import hashlib,json,pathlib,os
os.umask(0o077)
from django.apps import apps
from django.db.models import FileField
root=pathlib.Path('/backup'); files=[]; missing=[]; seen=set()
for model in apps.get_app_config('jobs').get_models():
 for field in model._meta.fields:
  if not isinstance(field,FileField): continue
  for obj in model.objects.exclude(**{field.name:''}).iterator():
   file=getattr(obj,field.name)
   if not file.name or file.name in seen: continue
   seen.add(file.name)
   if not file.storage.exists(file.name):
    missing.append({'name':file.name,'model':model._meta.label})
    continue
   with file.storage.open(file.name,'rb') as stream:
    data=stream.read()
   name='files/'+str(len(files))
   path=root/name
   path.parent.mkdir(exist_ok=True)
   path.write_bytes(data)
   files.append({'name':file.name,'backup':name,'sha256':hashlib.sha256(data).hexdigest()})
(root/'files.json').write_text(json.dumps(files))
(root/'missing-files.json').write_text(json.dumps(missing))
"""
    run(
        [
            "run",
            "--rm",
            "--no-deps",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{root}:/backup",
            "web",
            "python",
            "manage.py",
            "shell",
            "-c",
            script,
        ],
        stdout=subprocess.DEVNULL,
    )
    original = fingerprint("jobstream", args.admin_id)
    test_db = "pilot_restore_" + uuid.uuid4().hex
    run(["exec", "-T", "db", "createdb", "-U", "jobstream", test_db])
    try:
        with dump.open("rb") as stream:
            run(
                [
                    "exec",
                    "-T",
                    "db",
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "-U",
                    "jobstream",
                    "-d",
                    test_db,
                ],
                stdin=stream,
            )
        if original != fingerprint(test_db, args.admin_id):
            raise RuntimeError("Restored authentication fingerprint differs")
        # Verify full restoration counts, not just presence of the admin row.
        for table in ("auth_user", "jobs_source", "jobs_job", "jobs_document"):
            query = f"SELECT count(*) FROM {table}"
            counts = [
                run(
                    ["exec", "-T", "db", "psql", "-U", "jobstream", "-d", database, "-Atc", query],
                    capture_output=True,
                ).stdout
                for database in ("jobstream", test_db)
            ]
            if counts[0] != counts[1]:
                raise RuntimeError("Restored row count differs")
    finally:
        run(["exec", "-T", "db", "dropdb", "-U", "jobstream", test_db])
    manifest = {
        "admin_id": args.admin_id,
        "database_file": dump.name,
        "database_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "auth_fingerprint": original,
        "restore_verified": True,
        "files": json.loads((root / "files.json").read_text()),
        "missing_files": json.loads((root / "missing-files.json").read_text()),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(
        json.dumps(
            {
                "manifest": str(root / "manifest.json"),
                "restore_verified": True,
                "files": len(manifest["files"]),
                "preexisting_missing_files": len(manifest["missing_files"]),
            }
        )
    )


if __name__ == "__main__":
    main()
