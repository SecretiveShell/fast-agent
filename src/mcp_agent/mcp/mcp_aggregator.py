from asyncio import Lock, gather
from typing import List, Dict, Optional, TYPE_CHECKING
from mcp import GetPromptResult
from pydantic import BaseModel, ConfigDict
from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    Tool,
)

from mcp_agent.event_progress import ProgressAction
from mcp_agent.logging.logger import get_logger
from mcp_agent.mcp.gen_client import gen_client

from mcp_agent.context_dependent import ContextDependent
from mcp_agent.mcp.mcp_agent_client_session import MCPAgentClientSession
from mcp_agent.mcp.mcp_connection_manager import MCPConnectionManager

if TYPE_CHECKING:
    from mcp_agent.context import Context


logger = get_logger(
    __name__
)  # This will be replaced per-instance when agent_name is available

SEP = "-"


class NamespacedTool(BaseModel):
    """
    A tool that is namespaced by server name.
    """

    tool: Tool
    server_name: str
    namespaced_tool_name: str


class MCPAggregator(ContextDependent):
    """
    Aggregates multiple MCP servers. When a developer calls, e.g. call_tool(...),
    the aggregator searches all servers in its list for a server that provides that tool.
    """

    initialized: bool = False
    """Whether the aggregator has been initialized with tools and resources from all servers."""

    connection_persistence: bool = False
    """Whether to maintain a persistent connection to the server."""

    server_names: List[str]
    """A list of server names to connect to."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    async def __aenter__(self):
        if self.initialized:
            return self

        # Keep a connection manager to manage persistent connections for this aggregator
        if self.connection_persistence:
            # Try to get existing connection manager from context
            if not hasattr(self.context, "_connection_manager"):
                self.context._connection_manager = MCPConnectionManager(
                    self.context.server_registry
                )
                await self.context._connection_manager.__aenter__()
            self._persistent_connection_manager = self.context._connection_manager

        await self.load_servers()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def __init__(
        self,
        server_names: List[str],
        connection_persistence: bool = True,  # Default to True for better stability
        context: Optional["Context"] = None,
        name: str = None,
        **kwargs,
    ):
        """
        :param server_names: A list of server names to connect to.
        :param connection_persistence: Whether to maintain persistent connections to servers (default: True).
        Note: The server names must be resolvable by the gen_client function, and specified in the server registry.
        """
        super().__init__(
            context=context,
            **kwargs,
        )

        self.server_names = server_names
        self.connection_persistence = connection_persistence
        self.agent_name = name
        self._persistent_connection_manager: MCPConnectionManager = None

        # Set up logger with agent name in namespace if available
        global logger
        logger_name = f"{__name__}.{name}" if name else __name__
        logger = get_logger(logger_name)

        # Maps namespaced_tool_name -> namespaced tool info
        self._namespaced_tool_map: Dict[str, NamespacedTool] = {}
        # Maps server_name -> list of tools
        self._server_to_tool_map: Dict[str, List[NamespacedTool]] = {}
        self._tool_map_lock = Lock()

        # TODO: saqadri - add resources and prompt maps as well

    async def close(self):
        """
        Close all persistent connections when the aggregator is deleted.
        """
        if self.connection_persistence and self._persistent_connection_manager:
            try:
                # Only attempt cleanup if we own the connection manager
                if (
                    hasattr(self.context, "_connection_manager")
                    and self.context._connection_manager
                    == self._persistent_connection_manager
                ):
                    logger.info("Shutting down all persistent connections...")
                    await self._persistent_connection_manager.disconnect_all()
                    await self._persistent_connection_manager.__aexit__(
                        None, None, None
                    )
                    delattr(self.context, "_connection_manager")
                self.initialized = False
            except Exception as e:
                logger.error(f"Error during connection manager cleanup: {e}")

    @classmethod
    async def create(
        cls,
        server_names: List[str],
        connection_persistence: bool = False,
    ) -> "MCPAggregator":
        """
        Factory method to create and initialize an MCPAggregator.
        Use this instead of constructor since we need async initialization.
        If connection_persistence is True, the aggregator will maintain a
        persistent connection to the servers for as long as this aggregator is around.
        By default we do not maintain a persistent connection.
        """

        logger.info(f"Creating MCPAggregator with servers: {server_names}")

        instance = cls(
            server_names=server_names,
            connection_persistence=connection_persistence,
        )

        try:
            await instance.__aenter__()

            logger.debug("Loading servers...")
            await instance.load_servers()

            logger.debug("MCPAggregator created and initialized.")
            return instance
        except Exception as e:
            logger.error(f"Error creating MCPAggregator: {e}")
            await instance.__aexit__(None, None, None)

    async def load_servers(self):
        """
        Discover tools from each server in parallel and build an index of namespaced tool names.
        """
        if self.initialized:
            logger.debug("MCPAggregator already initialized.")
            return

        async with self._tool_map_lock:
            self._namespaced_tool_map.clear()
            self._server_to_tool_map.clear()

        for server_name in self.server_names:
            if self.connection_persistence:
                logger.info(
                    f"Creating persistent connection to server: {server_name}",
                    data={
                        "progress_action": ProgressAction.STARTING,
                        "server_name": server_name,
                        "agent_name": self.agent_name,
                    },
                )
                await self._persistent_connection_manager.get_server(
                    server_name, client_session_factory=MCPAgentClientSession
                )

            logger.info(
                f"MCP Servers initialized for agent '{self.agent_name}'",
                data={
                    "progress_action": ProgressAction.INITIALIZED,
                    "agent_name": self.agent_name,
                },
            )

        async def fetch_tools(client: ClientSession):
            try:
                result: ListToolsResult = await client.list_tools()
                return result.tools or []
            except Exception as e:
                logger.error(f"Error loading tools from server '{server_name}'", data=e)
                return []

        async def load_server_tools(server_name: str):
            tools: List[Tool] = []
            if self.connection_persistence:
                server_connection = (
                    await self._persistent_connection_manager.get_server(
                        server_name, client_session_factory=MCPAgentClientSession
                    )
                )
                tools = await fetch_tools(server_connection.session)
            else:
                async with gen_client(
                    server_name, server_registry=self.context.server_registry
                ) as client:
                    tools = await fetch_tools(client)

            return server_name, tools

        # Gather tools from all servers concurrently
        results = await gather(
            *(load_server_tools(server_name) for server_name in self.server_names),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, BaseException):
                continue
            server_name, tools = result

            self._server_to_tool_map[server_name] = []
            for tool in tools:
                namespaced_tool_name = f"{server_name}{SEP}{tool.name}"
                namespaced_tool = NamespacedTool(
                    tool=tool,
                    server_name=server_name,
                    namespaced_tool_name=namespaced_tool_name,
                )

                self._namespaced_tool_map[namespaced_tool_name] = namespaced_tool
                self._server_to_tool_map[server_name].append(namespaced_tool)
            logger.debug(
                "MCP Aggregator initialized",
                data={
                    "progress_action": ProgressAction.INITIALIZED,
                    "server_name": server_name,
                    "agent_name": self.agent_name,
                },
            )
        self.initialized = True

    async def list_servers(self) -> List[str]:
        """Return the list of server names aggregated by this agent."""
        if not self.initialized:
            await self.load_servers()

        return self.server_names

    async def list_tools(self) -> ListToolsResult:
        """
        :return: Tools from all servers aggregated, and renamed to be dot-namespaced by server name.
        """
        if not self.initialized:
            await self.load_servers()

        return ListToolsResult(
            tools=[
                namespaced_tool.tool.model_copy(update={"name": namespaced_tool_name})
                for namespaced_tool_name, namespaced_tool in self._namespaced_tool_map.items()
            ]
        )

    async def call_tool(
        self, name: str, arguments: dict | None = None
    ) -> CallToolResult:
        """
        Call a namespaced tool, e.g., 'server_name.tool_name'.
        """
        if not self.initialized:
            await self.load_servers()

        server_name: str = None
        local_tool_name: str = None

        if SEP in name:  # Namespaced tool name
            server_name, local_tool_name = name.split(SEP, 1)
        else:
            # Assume un-namespaced, loop through all servers to find the tool. First match wins.
            for _, tools in self._server_to_tool_map.items():
                for namespaced_tool in tools:
                    if namespaced_tool.tool.name == name:
                        server_name = namespaced_tool.server_name
                        local_tool_name = name
                        break

            if server_name is None or local_tool_name is None:
                logger.error(f"Error: Tool '{name}' not found")
                return CallToolResult(isError=True, message=f"Tool '{name}' not found")

        logger.info(
            "Requesting tool call",
            data={
                "progress_action": ProgressAction.CALLING_TOOL,
                "tool_name": local_tool_name,
                "server_name": server_name,
                "agent_name": self.agent_name,
            },
        )

        async def try_call_tool(client: ClientSession):
            try:
                return await client.call_tool(name=local_tool_name, arguments=arguments)
            except Exception as e:
                return CallToolResult(
                    isError=True,
                    message=f"Failed to call tool '{local_tool_name}' on server '{server_name}': {e}",
                )

        if self.connection_persistence:
            server_connection = await self._persistent_connection_manager.get_server(
                server_name, client_session_factory=MCPAgentClientSession
            )
            return await try_call_tool(server_connection.session)
        else:
            logger.debug(
                f"Creating temporary connection to server: {server_name}",
                data={
                    "progress_action": ProgressAction.STARTING,
                    "server_name": server_name,
                    "agent_name": self.agent_name,
                },
            )
            async with gen_client(
                server_name, server_registry=self.context.server_registry
            ) as client:
                result = await try_call_tool(client)
                logger.debug(
                    f"Closing temporary connection to server: {server_name}",
                    data={
                        "progress_action": ProgressAction.SHUTDOWN,
                        "server_name": server_name,
                        "agent_name": self.agent_name,
                    },
                )
                return result

    async def get_prompt(self, prompt_name: str = None) -> GetPromptResult:
        """
        Get a prompt from a server.
        
        :param prompt_name: Name of the prompt, optionally namespaced with server name 
                           using the format 'server_name-prompt_name'
        :return: GetPromptResult containing the prompt description and messages
        """
        if not self.initialized:
            await self.load_servers()
            
        server_name: str = None
        local_prompt_name: str = None
        
        if prompt_name and SEP in prompt_name:  # Namespaced prompt name
            server_name, local_prompt_name = prompt_name.split(SEP, 1)
        elif prompt_name:
            # If not namespaced, use the first server that has the prompt
            local_prompt_name = prompt_name
            server_name = self.server_names[0] if self.server_names else None
        else:
            # If no prompt name provided, use the first server's default prompt
            server_name = self.server_names[0] if self.server_names else None
            local_prompt_name = None
            
        if not server_name:
            logger.error("Error: No servers available for getting prompts")
            return GetPromptResult(
                description="Error: No servers available for getting prompts",
                messages=[],
            )
            
        logger.info(
            "Requesting prompt",
            data={
                "progress_action": ProgressAction.STARTING,
                "prompt_name": local_prompt_name,
                "server_name": server_name,
                "agent_name": self.agent_name,
            },
        )
        
        async def try_get_prompt(client: ClientSession):
            try:
                return await client.get_prompt(name=local_prompt_name)
            except Exception as e:
                logger.error(f"Failed to get prompt '{local_prompt_name}' from server '{server_name}': {e}")
                return GetPromptResult(
                    description=f"Error: Failed to get prompt '{local_prompt_name}' from server '{server_name}': {e}",
                    messages=[],
                )
                
        if self.connection_persistence:
            server_connection = await self._persistent_connection_manager.get_server(
                server_name, client_session_factory=MCPAgentClientSession
            )
            return await try_get_prompt(server_connection.session)
        else:
            logger.debug(
                f"Creating temporary connection to server: {server_name}",
                data={
                    "progress_action": ProgressAction.STARTING,
                    "server_name": server_name,
                    "agent_name": self.agent_name,
                },
            )
            async with gen_client(
                server_name, server_registry=self.context.server_registry
            ) as client:
                result = await try_get_prompt(client)
                logger.debug(
                    f"Closing temporary connection to server: {server_name}",
                    data={
                        "progress_action": ProgressAction.SHUTDOWN,
                        "server_name": server_name,
                        "agent_name": self.agent_name,
                    },
                )
                return result

    async def list_prompts(self, server_name: str = None):
        """
        List available prompts from one or all servers.
        
        :param server_name: Optional server name to list prompts from. If not provided, 
                           lists prompts from all servers.
        :return: Dictionary mapping server names to lists of available prompts
        """
        if not self.initialized:
            await self.load_servers()
            
        results = {}
        
        async def try_list_prompts(s_name, client):
            try:
                prompts = await client.list_prompts()
                return s_name, prompts
            except Exception as e:
                logger.error(f"Failed to list prompts from server '{s_name}': {e}")
                return s_name, []
                
        async def get_server_prompts(s_name):
            if self.connection_persistence:
                server_connection = await self._persistent_connection_manager.get_server(
                    s_name, client_session_factory=MCPAgentClientSession
                )
                s_name, prompts = await try_list_prompts(s_name, server_connection.session)
                return s_name, prompts
            else:
                async with gen_client(
                    s_name, server_registry=self.context.server_registry
                ) as client:
                    s_name, prompts = await try_list_prompts(s_name, client)
                    return s_name, prompts
        
        # If server_name is provided, only list prompts from that server
        if server_name:
            if server_name in self.server_names:
                s_name, prompts = await get_server_prompts(server_name)
                results[s_name] = prompts
            else:
                logger.error(f"Server '{server_name}' not found")
        else:
            # Gather prompts from all servers concurrently
            tasks = [get_server_prompts(s_name) for s_name in self.server_names]
            server_results = await gather(*tasks, return_exceptions=True)
            
            for result in server_results:
                if isinstance(result, BaseException):
                    continue
                s_name, prompts = result
                results[s_name] = prompts
                
        return results


class MCPCompoundServer(Server):
    """
    A compound server (server-of-servers) that aggregates multiple MCP servers and is itself an MCP server
    """

    def __init__(self, server_names: List[str], name: str = "MCPCompoundServer"):
        super().__init__(name)
        self.aggregator = MCPAggregator(server_names)

        # Register handlers for tools, prompts, and resources
        self.list_tools()(self._list_tools)
        self.call_tool()(self._call_tool)
        self.get_prompt()(self._get_prompt)
        self.list_prompts()(self._list_prompts)

    async def _list_tools(self) -> List[Tool]:
        """List all tools aggregated from connected MCP servers."""
        tools_result = await self.aggregator.list_tools()
        return tools_result.tools

    async def _call_tool(
        self, name: str, arguments: dict | None = None
    ) -> CallToolResult:
        """Call a specific tool from the aggregated servers."""
        try:
            result = await self.aggregator.call_tool(name=name, arguments=arguments)
            return result.content
        except Exception as e:
            return CallToolResult(isError=True, message=f"Error calling tool: {e}")
            
    async def _get_prompt(self, name: str = None) -> GetPromptResult:
        """Get a prompt from the aggregated servers."""
        try:
            result = await self.aggregator.get_prompt(prompt_name=name)
            return result
        except Exception as e:
            return GetPromptResult(
                description=f"Error getting prompt: {e}",
                messages=[]
            )
            
    async def _list_prompts(self, server_name: str = None) -> Dict[str, List[str]]:
        """List available prompts from the aggregated servers."""
        try:
            return await self.aggregator.list_prompts(server_name=server_name)
        except Exception as e:
            logger.error(f"Error listing prompts: {e}")
            return {}

    async def run_stdio_async(self) -> None:
        """Run the server using stdio transport."""
        async with stdio_server() as (read_stream, write_stream):
            await self.run(
                read_stream=read_stream,
                write_stream=write_stream,
                initialization_options=self.create_initialization_options(),
            )
