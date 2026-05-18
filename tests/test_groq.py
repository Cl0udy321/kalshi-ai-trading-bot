import asyncio
from src.clients.groq_client import GroqClient
from dotenv import load_dotenv

async def test_groq():
    load_dotenv()
    print("Testing connection to Groq API...")
    try:
        # Initialize the client. This uses the GROQ_API_KEY from .env
        client = GroqClient()
        
        # We test with a simple model and prompt
        response = await client.get_completion(
            prompt="Hello Groq! Respond with 'Hello, World!' to confirm you are online.",
            model="llama-3.1-8b-instant",
            max_tokens=50
        )
        
        if response:
            print("✅ Connection Successful!")
            print(f"🤖 Groq Response: {response}")
        else:
            print("❌ Connection failed or returned empty response.")
            
    except Exception as e:
        print("❌ Connection Failed. Check your GROQ_API_KEY in the .env file.")
        print(f"Error details: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(test_groq())
