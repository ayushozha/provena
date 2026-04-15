import argparse
import json

from app.config import get_settings
from app.connector_scheduler import ConnectorSchedulerService
from app.connector_worker import ConnectorExecutionService
from app.store import ProvenaStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Provena connected-mode scheduler tick.")
    parser.add_argument("--tenant-id", dest="tenant_id", help="Optional tenant scope for the scheduler tick.")
    parser.add_argument("--provider", help="Optional provider filter for the scheduler tick.")
    args = parser.parse_args()

    settings = get_settings()
    store = ProvenaStore(settings.resolved_db_path)
    try:
        scheduler = ConnectorSchedulerService(store, ConnectorExecutionService(store))
        result = scheduler.tick(tenant_id=args.tenant_id, provider=args.provider)
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
