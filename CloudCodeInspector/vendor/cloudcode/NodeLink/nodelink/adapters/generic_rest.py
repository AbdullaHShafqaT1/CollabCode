import aiohttp
import asyncio
from typing import Dict, Any, Optional
from .base_adapter import BaseAdapter
from ..models import ApiResponse, RemoteJobHandle, JobResult
from ..nodelog_stub import NodeLog

class GenericRestConnector(BaseAdapter):
    """
    General-purpose async HTTP/HTTPS client wrapper with built-in retry mechanisms,
    exponential backoff, and streaming support.
    """
    
    async def send_request(self, endpoint: str, method: str = "POST", 
                           data: Optional[Dict] = None, files: Optional[list] = None) -> ApiResponse:
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        max_retries = 3
        backoff_factor = 1.5
        
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    request_kwargs = {}
                    if data:
                        request_kwargs['json'] = data
                    # Note: files handling is simplified here for the generic adapter
                    
                    async with session.request(method, url, **request_kwargs) as response:
                        response_data = None
                        if response.content_type == 'application/json':
                            response_data = await response.json()
                        else:
                            response_data = await response.text()
                            
                        # Only return on successful or non-retriable errors
                        if response.status < 500:
                            return ApiResponse(
                                status_code=response.status,
                                headers=dict(response.headers),
                                data=response_data
                            )
                        else:
                            # 5xx errors are retriable
                            response.raise_for_status()
                            
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                NodeLog.warn(f"Request failed (attempt {attempt+1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return ApiResponse(status_code=500, headers={}, data={"error": str(e)})
                
                await asyncio.sleep(backoff_factor ** attempt)
                
        return ApiResponse(status_code=500, headers={}, data={"error": "Max retries exceeded"})

    async def run_remote(self, command_or_notebook: str, params: Optional[Dict] = None) -> RemoteJobHandle:
        """
        Generic remote execution via a hypothetical /execute endpoint.
        """
        payload = {"command": command_or_notebook, "params": params or {}}
        res = await self.send_request("execute", method="POST", data=payload)
        
        if res.status_code in [200, 201, 202]:
            job_id = res.data.get("job_id", "unknown_job")
            return RemoteJobHandle(target_id=self.target_id, job_id=job_id, status="SUBMITTED")
        raise RuntimeError(f"Failed to submit remote job: {res.data}")

    async def fetch_results(self, job_handle: RemoteJobHandle, download_path: Optional[str] = None) -> JobResult:
        """
        Generic status polling.
        """
        res = await self.send_request(f"jobs/{job_handle.job_id}", method="GET")
        if res.status_code == 200:
            status = res.data.get("status", "UNKNOWN")
            result_data = res.data.get("result", None)
            return JobResult(job_id=job_handle.job_id, status=status, result_data=result_data)
        
        return JobResult(job_id=job_handle.job_id, status="ERROR", error=str(res.data))
