from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app import dataset_sources

router = APIRouter(prefix="/api/settings/datasets", tags=["datasets"])          # administrator only (see app.auth)


@router.get("")
def dataset_status():
    """Whether each research dataset's list has been downloaded, and how big and old it is."""
    return dataset_sources.status()


@router.post("/{name}/update")
async def dataset_update(name: str):
    """Download (or refresh) a dataset's list. Only Hugging Face is contacted."""
    if name not in dataset_sources.NAMES:
        raise HTTPException(404, "Unknown dataset")
    try:
        return await run_in_threadpool(dataset_sources.update_thingi10k if name == "thingi10k" else dataset_sources.update_objaverse)
    except dataset_sources.DatasetError as e:
        raise HTTPException(502, str(e))


@router.delete("/{name}")
def dataset_remove(name: str):
    if name not in dataset_sources.NAMES:
        raise HTTPException(404, "Unknown dataset")
    dataset_sources.remove(name)
    return dataset_sources.status()
