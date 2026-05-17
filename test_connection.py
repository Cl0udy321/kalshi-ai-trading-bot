import os
from dotenv import load_dotenv
from kalshi_python import Configuration, KalshiClient

# Load your .env file
load_dotenv()

def test_kalshi_connection():
    print("Testing connection to Kalshi DEMO...")
    
    # 1. Load keys from your .env file
    key_id = os.getenv("KALSHI_API_KEY")
    secret_path = os.getenv("KALSHI_API_SECRET")
    
    # 2. Read the private key file
    try:
        with open(secret_path, "r") as file:
            private_key = file.read()
    except FileNotFoundError:
        print(f"❌ Error: Could not find your private key file at '{secret_path}'")
        return

    # 3. Configure the modern Kalshi Client for the Demo environment
    config = Configuration(host="https://demo-api.kalshi.co/trade-api/v2")
    config.api_key_id = key_id
    config.private_key_pem = private_key
    
    # 4. Initialize the connection
    client = KalshiClient(config)
    
    try:
        # Check your balance
        response = client.get_balance()
        print("✅ Connection Successful!")
        print(f"💰 Mock Bankroll Available: ${(response.balance / 100):.2f}")
    except Exception as e:
        print("❌ Connection Failed. Check your API Keys.")
        print(f"Error details: {e}")

if __name__ == "__main__":
    test_kalshi_connection()