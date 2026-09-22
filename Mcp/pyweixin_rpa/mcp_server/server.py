from fastmcp import FastMCP
from .tools.messaging import mcp as messaging_mcp
mcp=FastMCP("pyweixin_rpa")

# 挂载子模块
mcp.mount(messaging_mcp)

def main():
    mcp.run()

if __name__ == "__main__":
    main()