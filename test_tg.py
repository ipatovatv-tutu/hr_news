from telethon import TelegramClient
import asyncio

API_ID = 1234567          # твой api_id
API_HASH = "a1b2c3..."    # твой api_hash

client = TelegramClient("hr_news_session", API_ID, API_HASH)

async def test_channels():
    async with client:
        for ch in ["hr4you", "peopleanalytics"]:
            try:
                entity = await client.get_entity(ch)
                print(ch, entity.id)
            except Exception as e:
                print(f"Ошибка с каналом {ch}: {e}")

asyncio.run(test_channels())
