# LoadMatch

LoadMatch is an intelligent WhatsApp-based assistant for Indian truck owners and shippers. It enables seamless matchmaking between available truck space (capacity listings) and active load requests (shipments) by evaluating multiple criteria including date proximity, capacity fit, budget matching, and user KYC verification. All orchestration happens within a natural language WhatsApp conversation powered by Anthropic's Claude.

## Set up environment
1. Clone the project locally.
2. Ensure you have Docker and Docker-Compose installed.
3. Run `cp .env.example .env`.
4. Open the `.env` file and fill in the required keys:
   - `DATABASE_URL` (`postgresql+psycopg2://postgres:postgres@db:5432/loadmatch` for Docker Compose, or switch `db` to `localhost` if you run the app directly on your host)
   - `REDIS_URL` (`redis://redis:6379/0` for Docker Compose, or switch `redis` to `localhost` for host-native runs)
   - `WA_TOKEN` & `WA_PHONE_NUMBER_ID` & `WA_VERIFY_TOKEN` (From Facebook Developer Portal)
   - `ANTHROPIC_API_KEY` (Your Claude API Key)
   - `AWS_ACCESS_KEY`, `AWS_SECRET_KEY`, `AWS_REGION`, `S3_BUCKET` (For storing KYC credentials)

## Running Locally
1. Start the Docker containers:
```bash
docker-compose up -d --build
```
2. Apply database migrations to the deployed Postgres database:
```bash
source venv/bin/activate
alembic upgrade head
```
3. The server should now be running on port 8000 at `http://localhost:8000`.

## Connecting to WhatsApp Webhooks
To handle external internet traffic pointing at your local system, use ngrok or a similar local tunneling service:
1. Run `ngrok http 8000`. Keep this window open. 
2. Register the forwarding URL (e.g. `https://your-tunnel.ngrok-free.app/webhook`) in the Meta Developer Console under your WhatsApp App > Configuration > Webhook URL.
3. Configure the webhook to subscribe to `messages`. 
4. The Verification token must exactly match the `WA_VERIFY_TOKEN` variable in your `.env`.

## Example Usage Flows

### Flow 1: Shipper requests a load
**User:** Hello  
**Assistant:** Hello! Since you are new to LoadMatch, could you please tell me your name and whether you are a Shipper, Transporter, or Both?  
**User:** I am a Shipper, my name is Amit.  
**Assistant:** Nice to meet you Amit. How can I help you today?  
**User:** I have 500kg of tiles I need to send from Delhi to Mumbai tomorrow. My budget is 15/kg.  
**Assistant:** Great, I've created your load request id! I also checked our system and found a truck match... *(sends load options)*

### Flow 2: Transporter offers truck space
**User:** I have an empty truck tomorrow going from Delhi to Mumbai.  
**Assistant:** Hello! Let's get you set up first. What is your name and are you a Shipper/Transporter?  
**User:** Transporter, name is Rahul.  
**Assistant:** Hi Rahul! What's your truck's registration number?  
**User:** MH12AB3456. It has 1000kg space, asking price is 10/kg.  
**Assistant:** Thanks Rahul! Your truck space has been listed successfully!

*Note: Uploading images of Aadhaar / PAN to WhatsApp automatically triggers the KYC extraction tools.*
