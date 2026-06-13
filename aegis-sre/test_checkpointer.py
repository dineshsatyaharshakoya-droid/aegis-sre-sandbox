import asyncio
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async def main():
    async with AsyncSqliteSaver.from_conn_string("test.db") as checkpointer:
        print("Checkpointer initialized:", checkpointer)

if __name__ == "__main__":
    asyncio.run(main())
