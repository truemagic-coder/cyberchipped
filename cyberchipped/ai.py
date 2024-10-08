import json
from datetime import datetime
from typing import AsyncGenerator, Literal, Optional, Dict, Any, Callable
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
from openai import OpenAI
import openai
import aiosqlite
from openai import AssistantEventHandler
from openai.types.beta.threads import TextDelta, Text
from typing_extensions import override
import sqlite3
import inspect


def adapt_datetime(ts):
    return ts.isoformat()


# Custom converter for datetime
def convert_datetime(ts):
    return datetime.fromisoformat(ts)


# Register the adapter and converter
sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)


class EventHandler(AssistantEventHandler):
    def __init__(self, tool_handlers, ai_instance):
        super().__init__()
        self.tool_handlers = tool_handlers
        self.ai_instance = ai_instance
        self.accumulated_value = ""

    @override
    def on_text_delta(self, delta: TextDelta, snapshot: Text):
        self.ai_instance.accumulated_value += delta.value

    @override
    def on_event(self, event):
        if event.event == "thread.run.requires_action":
            run_id = event.data.id  # Retrieve the run ID from the event data
            self.ai_instance.handle_requires_action(event.data, run_id)


class ToolConfig(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]


class MongoDatabase:
    def __init__(self, db_url: str, db_name: str):
        self.client = AsyncIOMotorClient(db_url)
        self.db = self.client[db_name]
        self.threads = self.db["threads"]
        self.messages = self.db["messages"]

    async def save_thread_id(self, user_id: str, thread_id: str):
        await self.threads.insert_one({"thread_id": thread_id, "user_id": user_id})

    async def get_thread_id(self, user_id: str) -> Optional[str]:
        document = await self.threads.find_one({"user_id": user_id})
        return document["thread_id"] if document else None

    async def save_message(self, user_id: str, metadata: Dict[str, Any]):
        metadata["user_id"] = user_id
        await self.messages.insert_one(metadata)

    async def delete_thread_id(self, user_id: str):
        document = await self.threads.find_one({"user_id": user_id})
        thread_id = document["thread_id"]
        openai.beta.threads.delete(thread_id)
        await self.messages.delete_many({"user_id": user_id})
        await self.threads.delete_one({"user_id": user_id})


class SQLiteDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads (user_id TEXT, thread_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (user_id TEXT, message TEXT, response TEXT, timestamp TEXT)"
        )
        conn.commit()
        conn.close()

    async def save_thread_id(self, user_id: str, thread_id: str):
        async with aiosqlite.connect(
            self.db_path, detect_types=sqlite3.PARSE_DECLTYPES
        ) as db:
            await db.execute(
                "INSERT INTO threads (user_id, thread_id) VALUES (?, ?)",
                (user_id, thread_id),
            )
            await db.commit()

    async def get_thread_id(self, user_id: str) -> Optional[str]:
        async with aiosqlite.connect(
            self.db_path, detect_types=sqlite3.PARSE_DECLTYPES
        ) as db:
            async with db.execute(
                "SELECT thread_id FROM threads WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def save_message(self, user_id: str, metadata: Dict[str, Any]):
        async with aiosqlite.connect(
            self.db_path, detect_types=sqlite3.PARSE_DECLTYPES
        ) as db:
            await db.execute(
                "INSERT INTO messages (user_id, message, response, timestamp) VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    metadata["message"],
                    metadata["response"],
                    metadata["timestamp"],
                ),
            )
            await db.commit()

    async def delete_thread_id(self, user_id: str):
        async with aiosqlite.connect(
            self.db_path, detect_types=sqlite3.PARSE_DECLTYPES
        ) as db:
            async with db.execute(
                "SELECT thread_id FROM threads WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    thread_id = row[0]
                    openai.beta.threads.delete(thread_id)
                    await db.execute(
                        "DELETE FROM messages WHERE user_id = ?", (user_id,)
                    )
                    await db.execute(
                        "DELETE FROM threads WHERE user_id = ?", (user_id,)
                    )
                    await db.commit()


class AI:
    def __init__(self, api_key: str, name: str, instructions: str, database: Any, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=api_key)
        self.name = name
        self.instructions = instructions
        self.model = model
        self.tools = [{"type": "code_interpreter"}]
        self.tool_handlers = {}
        self.assistant_id = None
        self.database = database
        self.accumulated_value = ""

    async def __aenter__(self):
        assistants = openai.beta.assistants.list()
        existing_assistant = next(
            (a for a in assistants if a.name == self.name), None)

        if existing_assistant:
            self.assistant_id = existing_assistant.id
        else:
            self.assistant_id = openai.beta.assistants.create(
                name=self.name,
                instructions=self.instructions,
                tools=self.tools,
                model=self.model,
            ).id

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Perform any cleanup actions here
        pass

    async def create_thread(self, user_id: str) -> str:
        thread_id = await self.database.get_thread_id(user_id)

        if thread_id is None:
            thread = openai.beta.threads.create()
            thread_id = thread.id
            await self.database.save_thread_id(user_id, thread_id)

        return thread_id

    async def listen(self, audio_content: bytes, input_format: str) -> str:
        transcription = self.client.audio.transcriptions.create(
            model="whisper-1",
            file=(f"file.{input_format}", audio_content),
        )
        return transcription.text

    async def text(self, user_id: str, text: str) -> AsyncGenerator[str, None]:
        thread_id = await self.database.get_thread_id(user_id)

        if thread_id is None:
            thread_id = await self.create_thread(user_id)

        self.current_thread_id = thread_id

        # Create a message in the thread
        self.client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=text,
        )

        accumulated_response = []

        class CustomEventHandler(EventHandler):
            def __init__(self, tool_handlers, ai_instance, accumulated_response):
                super().__init__(tool_handlers, ai_instance)
                self.accumulated_response = accumulated_response

            def on_text_delta(self, delta, snapshot):
                self.accumulated_response.append(delta.value)
                self.ai_instance.accumulated_value += delta.value

        event_handler = CustomEventHandler(
            self.tool_handlers, self, accumulated_response)

        with self.client.beta.threads.runs.stream(
            thread_id=thread_id,
            assistant_id=self.assistant_id,
            event_handler=event_handler,
        ) as stream:
            for text in stream.text_deltas:
                yield text
            stream.until_done()

        response_text = "".join(accumulated_response)

        # Save the message to the database
        metadata = {
            "user_id": user_id,
            "message": text,
            "response": response_text,
            "timestamp": datetime.now(),
        }

        await self.database.save_message(user_id, metadata)

    async def conversation(
        self,
        user_id: str,
        audio_bytes: bytes,
        voice: Literal["alloy", "echo", "fable",
                       "onyx", "nova", "shimmer"] = "nova",
        input_format: Literal["flac", "m4a", "mp3", "mp4",
                              "mpeg", "mpga", "oga", "ogg", "wav", "webm"] = "mp4",
        response_format: Literal["mp3", "opus",
                                 "aac", "flac", "wav", "pcm"] = "aac",
    ) -> AsyncGenerator[bytes, None]:
        thread_id = await self.database.get_thread_id(user_id)

        if thread_id is None:
            thread_id = await self.create_thread(user_id)

        self.current_thread_id = thread_id
        transcript = await self.listen(audio_bytes, input_format)
        event_handler = EventHandler(self.tool_handlers, self)
        openai.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=transcript,
        )

        with openai.beta.threads.runs.stream(
            thread_id=thread_id,
            assistant_id=self.assistant_id,
            event_handler=event_handler,
        ) as stream:
            stream.until_done()

        response_text = self.accumulated_value

        metadata = {
            "user_id": user_id,
            "message": transcript,
            "response": response_text,
            "timestamp": datetime.now(),
        }

        await self.database.save_message(user_id, metadata)

        with self.client.audio.speech.with_streaming_response.create(
            model="tts-1",
            voice=voice,
            input=response_text,
            response_format=response_format,
        ) as response:
            for chunk in response.iter_bytes(1024):
                yield chunk

    def handle_requires_action(self, data, run_id):
        tool_outputs = []

        for tool in data.required_action.submit_tool_outputs.tool_calls:
            if tool.function.name in self.tool_handlers:
                handler = self.tool_handlers[tool.function.name]
                inputs = json.loads(tool.function.arguments)
                output = handler(**inputs)
                tool_outputs.append(
                    {"tool_call_id": tool.id, "output": output})

        # Submit all tool_outputs at the same time
        self.submit_tool_outputs(tool_outputs, run_id)

    def submit_tool_outputs(self, tool_outputs, run_id):
        # Use the submit_tool_outputs_stream helper
        with openai.beta.threads.runs.submit_tool_outputs_stream(
            thread_id=self.current_thread_id,
            run_id=run_id,
            tool_outputs=tool_outputs,
            event_handler=EventHandler(self.tool_handlers, self),
        ) as stream:
            for text in stream.text_deltas:
                print(text, end="", flush=True)
            print()

    def add_tool(self, func: Callable):
        sig = inspect.signature(func)
        parameters = {"type": "object", "properties": {}, "required": []}
        for name, param in sig.parameters.items():
            parameters["properties"][name] = {
                "type": "string", "description": "foo"}
            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)
        tool_config = {
            "type": "function",
            "function": {
                "name": func.__name__,
                "description": func.__doc__ or "",
                "parameters": parameters,
            },
        }
        self.tools.append(tool_config)
        self.tool_handlers[func.__name__] = func
        return func


tool = AI.add_tool
