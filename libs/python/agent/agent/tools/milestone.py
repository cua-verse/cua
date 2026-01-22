"""
Milestone Tool for saving screenshots on the remote computer.
"""

import base64
import logging
from typing import TYPE_CHECKING, Optional, Union

from .base import BaseTool, register_tool

if TYPE_CHECKING:
    from computer.interface import BaseComputerInterface

logger = logging.getLogger(__name__)


@register_tool("save_milestone_screenshot")
class MilestoneTool(BaseTool):
    """
    Tool for saving milestone screenshots on the remote computer.
    """

    def __init__(self, interface: "BaseComputerInterface", cfg: Optional[dict] = None):
        """
        Initialize the MilestoneTool.

        Args:
            interface: A BaseComputerInterface instance
            cfg: Optional configuration dictionary
        """
        self.interface = interface
        super().__init__(cfg)

    @property
    def description(self) -> str:
        return "Save the current screen as a milestone screenshot on the remote computer. Use this when you have completed a significant step or goal and want to save evidence of your progress."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path on the remote computer where the screenshot should be saved. Example: 'C:/Users/User/Desktop/milestones/step1.png'",
                },
                "description": {
                    "type": "string",
                    "description": "A brief description of the milestone achieved.",
                },
            },
            "required": ["path"],
        }

    def call(self, params: Union[str, dict], **kwargs) -> Union[str, dict]:
        """
        Execute the milestone screenshot save.

        Args:
            params: Action parameters (JSON string or dict)
            **kwargs: Additional keyword arguments

        Returns:
            Result of the action execution
        """
        import asyncio
        import concurrent.futures

        # Verify and parse parameters
        params_dict = self._verify_json_format_args(params)
        path = params_dict.get("path")
        description = params_dict.get("description", "")

        if not path:
            return {"success": False, "error": "path parameter is required"}

        # Execute action synchronously by running async method in event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're already in an async context, we can't use run_until_complete
                # Create a task and wait for it
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run, self._execute_save(path, description)
                    )
                    result = future.result()
            else:
                result = loop.run_until_complete(self._execute_save(path, description))
            return result
        except Exception as e:
            logger.error(f"Error saving milestone screenshot: {e}")
            return {"success": False, "error": str(e)}

    async def _execute_save(self, path: str, description: str) -> dict:
        """Execute the screenshot save asynchronously."""
        try:
            # 1. Take screenshot
            screenshot_bytes = await self.interface.screenshot()
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            # 2. Prepare remote directory
            dir_path = "/".join(path.rsplit("/", 1)[:-1]) if "/" in path else "."
            if dir_path and dir_path != ".":
                await self.interface.run_command(f'mkdir -p "{dir_path}"')

            # 3. Save file on remote computer using python (more reliable for binary data)
            save_cmd = f'''python3 -c "
import base64
data = base64.b64decode('{screenshot_b64}')
with open('{path}', 'wb') as f:
    f.write(data)
print('SUCCESS')
"'''
            result = await self.interface.run_command(save_cmd)

            if "SUCCESS" in (result.stdout or ""):
                msg = f"✅ Milestone screenshot saved to: {path}"
                if description:
                    msg += f" (Milestone: {description})"
                return {"success": True, "message": msg}
            else:
                return {
                    "success": False,
                    "error": f"Failed to save screenshot: {result.stderr or result.stdout}",
                }

        except Exception as e:
            return {"success": False, "error": str(e)}
