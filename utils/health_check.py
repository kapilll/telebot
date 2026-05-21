import asyncio
from typing import Dict, Any, Optional
import MetaTrader5 as mt5


async def health_check(db, account_manager, logger) -> Dict[str, Any]:
    """
    Check the health of all critical bot components.

    Args:
        db: Database instance
        account_manager: AccountManager instance with MT5 accounts
        logger: Logger instance

    Returns:
        Dict with health status of each component:
        {
            "status": "OK" or "DEGRADED" or "FAIL",
            "timestamp": ISO timestamp,
            "db": "OK" or "FAIL",
            "accounts": {
                "account_name": "OK" or "FAIL"
            },
            "details": {
                "db": error message if failed,
                "accounts": {
                    "account_name": error message if failed
                }
            }
        }
    """
    from datetime import datetime

    health_status = {
        "status": "OK",
        "timestamp": datetime.utcnow().isoformat(),
        "db": "OK",
        "accounts": {},
        "details": {
            "db": None,
            "accounts": {}
        }
    }

    # Check database
    try:
        if db.connection is None:
            health_status["db"] = "FAIL"
            health_status["details"]["db"] = "Database connection not initialized"
            health_status["status"] = "FAIL"
            logger.error("Health check: DB connection not initialized")
        else:
            # Try a simple query
            cursor = await db.connection.execute("SELECT 1")
            await cursor.close()
            health_status["db"] = "OK"
            logger.debug("Health check: DB=OK")
    except Exception as e:
        health_status["db"] = "FAIL"
        health_status["details"]["db"] = str(e)
        health_status["status"] = "FAIL"
        logger.error(f"Health check: DB=FAIL ({e})")

    # Check MT5 accounts
    if hasattr(account_manager, 'accounts') and account_manager.accounts:
        for account_name, executor in account_manager.accounts.items():
            try:
                if not executor.connected:
                    health_status["accounts"][account_name] = "FAIL"
                    health_status["details"]["accounts"][account_name] = "MT5 not connected"
                    health_status["status"] = "DEGRADED"
                    logger.warning(f"Health check: {account_name}=FAIL (not connected)")
                else:
                    # Verify MT5 is still responsive
                    if mt5.initialize(path=executor.mt5_path):
                        health_status["accounts"][account_name] = "OK"
                        logger.debug(f"Health check: {account_name}=OK")
                    else:
                        health_status["accounts"][account_name] = "FAIL"
                        error_msg = f"MT5 initialization failed: {mt5.last_error()}"
                        health_status["details"]["accounts"][account_name] = error_msg
                        health_status["status"] = "DEGRADED"
                        logger.warning(f"Health check: {account_name}=FAIL ({error_msg})")
            except Exception as e:
                health_status["accounts"][account_name] = "FAIL"
                health_status["details"]["accounts"][account_name] = str(e)
                health_status["status"] = "DEGRADED"
                logger.error(f"Health check: {account_name}=FAIL ({e})")
    else:
        logger.warning("Health check: No MT5 accounts configured")

    # Log summary
    summary_parts = [f"DB={health_status['db']}"]
    for account_name, status in health_status["accounts"].items():
        summary_parts.append(f"{account_name}={status}")

    summary = "Health check: " + ", ".join(summary_parts)
    if health_status["status"] == "OK":
        logger.info(summary)
    elif health_status["status"] == "DEGRADED":
        logger.warning(summary)
    else:
        logger.error(summary)

    return health_status
