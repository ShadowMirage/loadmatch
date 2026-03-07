import json
from anthropic import AsyncAnthropic
from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User
from app.models.conversation import Conversation
from app.tools.bot_tools import TOOLS, execute_tool

SYSTEM_PROMPT = """You are the LoadMatch assistant on WhatsApp — a smart, friendly helper for Indian truck owners and shippers.

Your only goals:
1. Help shippers post load requests (goods to send from city A to city B)
2. Help transporters post available truck space (empty/partial truck going A to B)  
3. Show matches and help confirm them
4. Help with KYC document uploads

Rules:
- Ask only ONE question at a time
- Keep all messages SHORT — this is WhatsApp, not email
- If user is new (no name or role set), first ask their name then whether they are a Shipper, Transporter, or Both — do this before anything else
- If user sends [IMAGE:media_id] in their message, ask them which document it is (Aadhaar/PAN/RC Book/Driving License/GST) then call save_kyc_document
- Confirm details with user before calling any create_ tool
- Respond in the same language the user writes in (Hindi or English)
- Use ₹ symbol for prices, not dollars
- Never mention internal IDs to users, use friendly references like 'your load from Delhi to Mumbai'

User context:
Phone: {phone}
Name: {name}
Role: {role}
KYC Status: {kyc_status}"""

MODEL_NAME = "claude-3-5-sonnet-20241022"  # Using available sonnet class locally depending on anthropic versions usually mapped

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

async def handle_message(phone: str, text: str, db: Session) -> str:
    # 1. Get or create User
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        user = User(phone=phone)
        db.add(user)
        db.commit()
        db.refresh(user)
        
    # 2. Load last 20 conversation messages
    history_records = (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id)
        .filter(Conversation.session_id == phone)
        .order_by(Conversation.created_at.asc())
        .limit(20)
        .all()
    )
    
    # 3. Build messages list
    messages = []
    for record in history_records:
        # Load string content back into anthropic dict blocks if possible
        # for simplicity we treat user and assistant block as text directly.
        try:
             messages.append({"role": record.role, "content": json.loads(record.content)})
        except:
             messages.append({"role": record.role, "content": record.content})

    # Add new user message
    messages.append({"role": "user", "content": text})
    
    # Save the incoming user message to DB
    user_conv = Conversation(
        user_id=user.id,
        session_id=phone,
        role="user",
        content=text
    )
    db.add(user_conv)
    
    # 4. Fill SYSTEM_PROMPT
    system_filled = SYSTEM_PROMPT.format(
        phone=phone,
        name=user.name or "Unknown",
        role=user.role.value if user.role else "Unknown",
        kyc_status=user.kyc_status.value
    )
    
    # 5. Call Claude with tools loop
    while True:
        response = await client.messages.create(
            model=MODEL_NAME,
            max_tokens=1000,
            system=system_filled,
            messages=messages,
            tools=TOOLS
        )
        
        messages.append({"role": "assistant", "content": response.content})
        
        if response.stop_reason == "tool_use":
            tool_results = []
            
            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_args = block.input
                    
                    try:
                        result = await execute_tool(tool_name, tool_args, db, user)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result)
                        })
                    except Exception as e:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {str(e)}",
                            "is_error": True
                        })
            
            messages.append({"role": "user", "content": tool_results})
            # Loop will continue and call Claude again
        else:
            break
            
    # 6. Extract final text reply
    final_text = ""
    for block in response.content:
        if block.type == "text":
            final_text += block.text
            
    # 7. Save assistant reply to Conversation table
    # We serialize the entire final message block to keep tool definitions safe
    # Though usually we can just store the final text layout too
    assistant_conv = Conversation(
        user_id=user.id,
        session_id=phone,
        role="assistant",
        content=final_text
    )
    db.add(assistant_conv)
    db.commit()
    
    # 8. Return reply string
    return final_text
