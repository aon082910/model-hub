from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session

from app import activity, duplicates
from app.db import get_session
from app.library_maintenance import (
    acquire_library_maintenance, current_library_maintenance, release_library_maintenance,
)

router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


def _exclusive():
    if not acquire_library_maintenance("duplicates"):
        raise HTTPException(409, f"Another task is running ({current_library_maintenance() or 'maintenance'}); try again when it has finished")


@router.get("")
def list_duplicates(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=duplicates.MAX_GROUPS),
                    session: Session = Depends(get_session)):
    """Groups of identical files, each with what is attached to every copy and which one to keep."""
    return duplicates.find_groups(session, offset, limit)


@router.get("/similar")
def list_similar(session: Session = Depends(get_session)):
    """Files with the same shape but different contents. For looking at only."""
    return {"groups": duplicates.find_similar(session)}


@router.post("/merge")
def merge_group(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Keep one model and clean up its exact copies. delete_files must be true to delete anything from disk."""
    keep_id = payload.get("keep_id")
    if not isinstance(keep_id, int) or isinstance(keep_id, bool):
        raise HTTPException(400, "keep_id must be a model id")
    delete_files = payload.get("delete_files") is True
    _exclusive()
    try:
        result = duplicates.merge(session, keep_id, payload.get("remove_ids") or [], delete_files)
        activity.record(session, activity.actor_of(request), "duplicates",
                        f"Cleaned up duplicates of model {keep_id}: {result['merged']} merged, {result['deleted_files']} file(s) deleted")
        return result
    except duplicates.DuplicateError as e:
        raise HTTPException(400, str(e))
    finally:
        release_library_maintenance()


@router.post("/merge-all")
def merge_all(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Clean up every group of copies, keeping the suggested model in each. Deleting files from disk
    needs confirm == "delete"; without it the copies only share their tags, collections and notes."""
    delete_files = payload.get("delete_files") is True
    if delete_files and payload.get("confirm") != "delete":
        raise HTTPException(400, 'Deleting files needs confirm="delete"')
    _exclusive()
    try:
        total = {"groups": 0, "merged": 0, "deleted_files": 0, "removed_records": 0, "freed_bytes": 0, "skipped": []}
        seen = set()
        while True:
            page = duplicates.find_groups(session, 0, duplicates.MAX_GROUPS)
            progress = False
            for group in page["groups"]:
                if group["hash"] in seen:
                    continue
                seen.add(group["hash"])
                keep = group["suggested_keep"]
                others = [m["id"] for m in group["models"] if m["id"] != keep]
                result = duplicates.merge(session, keep, others, delete_files)
                total["groups"] += 1
                for key in ("merged", "deleted_files", "removed_records", "freed_bytes"):
                    total[key] += result[key]
                total["skipped"] += result["skipped"]
                progress = progress or result["removed_records"] > 0
            # without deleting, the groups never shrink; one pass is all there is. With deleting,
            # go again while there are more groups than one page holds.
            if not delete_files or not progress or page["total"] <= duplicates.MAX_GROUPS:
                break
        activity.record(session, activity.actor_of(request), "duplicates",
                        f"Cleaned up every group of duplicates: {total['merged']} merged, {total['deleted_files']} file(s) deleted")
        return total
    except duplicates.DuplicateError as e:
        raise HTTPException(400, str(e))
    finally:
        release_library_maintenance()
