import os
import time
import re
import streamlit as st
from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

# Load environment variables
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")

# Initialize Clients
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

# --- Custom Styling for Premium Look ---
st.set_page_config(page_title="AI Assistant", page_icon="🤖", layout="centered")

# --- System Prompt Definition ---
SYSTEM_PROMPT = """You are a helpful customer support AI assistant.
Your role is to assist the user with their queries professionally.

ESCALATION PROTOCOL:
If the user explicitly asks to speak to a human, a real agent, support, OR if you do not know the answer to their question, you must follow this strict protocol:
1. Do NOT escalate immediately.
2. First, politely ask the user to provide their email address so a human agent can follow up with them.
3. If the user refuses to provide an email, politely explain that it is strictly required to connect them to an agent.
4. Once the user provides a valid email address, you must trigger the escalation by outputting EXACTLY this format at the very end of your message:
[ESCALATE: user@example.com]
(Replace "user@example.com" with the actual email address provided by the user).

CRITICAL RULES:
- Never output the [ESCALATE: ...] tag until the user has provided a valid email address.
- A valid email MUST contain an '@' symbol AND a valid domain extension (like '.com', '.org', '.net'). For example, 'test@abc' is INVALID. 'test@abc.com' is VALID.
- If the user provides an invalid email format, politely tell them it looks invalid and ask them to provide a correct one.
- STRICT LOCK: Once the escalation process has started (i.e. you have asked for their email), you MUST ignore all other topics or questions from the user. You must strictly insist on getting their valid email address before proceeding. Do not change the subject.
- Handle the entire conversation naturally and conversationally.
"""

# --- Session State Initialization ---
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Hello! I am your AI assistant. How can I help you today?", "is_slack": False}
    ]

# States: NORMAL (LLM Handles it), WAITING_FOR_AGENT, IN_HUMAN_CHAT
if "escalation_state" not in st.session_state:
    st.session_state.escalation_state = "NORMAL"

if "slack_thread_ts" not in st.session_state:
    st.session_state.slack_thread_ts = None

if "last_slack_message_ts" not in st.session_state:
    st.session_state.last_slack_message_ts = None

if "slack_users_cache" not in st.session_state:
    st.session_state.slack_users_cache = {}


def get_slack_user_info(user_id):
    """Fetch user profile from Slack and cache it."""
    if user_id in st.session_state.slack_users_cache:
        return st.session_state.slack_users_cache[user_id]
    
    try:
        response = slack_client.users_info(user=user_id)
        user_info = response["user"]
        profile = {
            "name": user_info["profile"].get("real_name", user_info.get("name")),
            "avatar": user_info["profile"].get("image_48")
        }
        st.session_state.slack_users_cache[user_id] = profile
        return profile
    except SlackApiError as e:
        print(f"Error fetching user info: {e.response['error']}")
        return {"name": "Support Agent", "avatar": "https://api.dicebear.com/7.x/bottts/svg?seed=support"}

def trigger_escalation(email):
    """Send the chat history and email to Slack to start a thread."""
    if not slack_client or not SLACK_CHANNEL_ID:
        st.error("Slack credentials are missing! Cannot escalate.")
        return

    st.session_state.escalation_state = "WAITING_FOR_AGENT"
    
    # Build the transcript
    transcript = f"*User has requested human assistance!*\n*Email:* {email}\n\n*Chat History:*\n"
    for msg in st.session_state.messages[-6:]: # Send last 6 messages for context
        if not msg.get("is_slack"):
            role = "User" if msg["role"] == "user" else "Bot"
            transcript += f"*{role}*: {msg['content']}\n"

    try:
        response = slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=transcript,
            reply_broadcast=False
        )
        # Save the thread timestamp to listen to replies
        st.session_state.slack_thread_ts = response["ts"]
        
    except SlackApiError as e:
        st.error(f"Error posting to Slack: {e.response['error']}")

def check_slack_replies():
    """Poll Slack for new messages in the thread."""
    if not st.session_state.slack_thread_ts or not slack_client:
        return

    try:
        response = slack_client.conversations_replies(
            channel=SLACK_CHANNEL_ID,
            ts=st.session_state.slack_thread_ts
        )
        messages = response["messages"]
        
        new_reply_found = False
        
        # Skip the first message (which is our own bot's transcript post)
        for msg in messages[1:]:
            msg_ts = msg["ts"]
            
            # We only care about new messages we haven't seen yet
            if st.session_state.last_slack_message_ts is None or float(msg_ts) > float(st.session_state.last_slack_message_ts):
                
                # Ignore messages sent by the bot itself (user messages forwarded)
                if msg.get("bot_id"):
                    continue
                
                user_info = get_slack_user_info(msg["user"])
                
                # Append the human agent's reply to Streamlit chat
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": msg["text"],
                    "is_slack": True,
                    "slack_name": user_info["name"],
                    "slack_avatar": user_info["avatar"]
                })
                
                st.session_state.last_slack_message_ts = msg_ts
                new_reply_found = True
        
        # If we found a human reply and we were just waiting, transition to fully connected
        if new_reply_found and st.session_state.escalation_state == "WAITING_FOR_AGENT":
            st.session_state.escalation_state = "IN_HUMAN_CHAT"
                
    except SlackApiError as e:
        print(f"Error fetching replies: {e.response['error']}")


# --- UI Layout ---
st.title("Customer Support 💬")

# If we are waiting for an agent or in a human chat, poll Slack every 4 seconds
if st.session_state.escalation_state in ["WAITING_FOR_AGENT", "IN_HUMAN_CHAT"]:
    st_autorefresh(interval=4000, key="slack_poller")
    check_slack_replies()

# Display chat history
for message in st.session_state.messages:
    if message.get("is_slack"):
        # Human agent reply
        with st.chat_message("assistant", avatar=message.get("slack_avatar")):
            st.markdown(f"**{message.get('slack_name')} (Human)**")
            st.markdown(message["content"])
    else:
        # User or standard Bot reply
        if "[ESCALATE:" not in message["content"]: # Hide the internal tag from history
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

# Chat Input
if prompt := st.chat_input("Type your message..."):
    # Display user message
    st.session_state.messages.append({"role": "user", "content": prompt, "is_slack": False})
    with st.chat_message("user"):
        st.markdown(prompt)

    # STATE: NORMAL (LLM Handles Everything)
    if st.session_state.escalation_state == "NORMAL":
        if openai_client:
            with st.chat_message("assistant"):
                message_placeholder = st.empty()
                full_response = ""
                
                # Build context for LLM with System Prompt
                openai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                openai_messages.extend([{"role": m["role"], "content": m["content"]} for m in st.session_state.messages if not m.get("is_slack")])
                
                try:
                    stream = openai_client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=openai_messages,
                        stream=True,
                    )
                    for chunk in stream:
                        if chunk.choices[0].delta.content is not None:
                            full_response += chunk.choices[0].delta.content
                            # Hide the tag from the UI while streaming
                            display_text = re.sub(r'\[ESCALATE:.*\]', '', full_response)
                            message_placeholder.markdown(display_text + "▌")
                    
                    # Check if the LLM decided to escalate
                    escalate_match = re.search(r'\[ESCALATE:\s*(.+?)\]', full_response)
                    
                    if escalate_match:
                        email = escalate_match.group(1).strip()
                        
                        # Show the LLM's natural response, just hide the internal tag
                        clean_response = re.sub(r'\[ESCALATE:.*\]', '', full_response).strip()
                        message_placeholder.markdown(clean_response)
                        st.session_state.messages.append({"role": "assistant", "content": clean_response, "is_slack": False})
                        
                        trigger_escalation(email)
                        st.rerun()
                    else:
                        message_placeholder.markdown(full_response)
                        st.session_state.messages.append({"role": "assistant", "content": full_response, "is_slack": False})
                except Exception as e:
                    st.error(f"OpenAI Error: {e}")
        else:
            st.error("OpenAI API Key is not set!")

    # STATE: WAITING_FOR_AGENT
    elif st.session_state.escalation_state == "WAITING_FOR_AGENT":
        # Send user's message to Slack so the agent sees it when they arrive
        try:
            slack_client.chat_postMessage(
                channel=SLACK_CHANNEL_ID,
                thread_ts=st.session_state.slack_thread_ts,
                text=f"*User*: {prompt}"
            )
        except SlackApiError as e:
            st.error(f"Error sending message to Slack: {e.response['error']}")
            
        # Use LLM to generate a dynamic waiting message
        if openai_client:
            with st.chat_message("assistant"):
                message_placeholder = st.empty()
                full_response = ""
                
                wait_prompt = "The user is currently waiting for a human agent to join the chat. Acknowledge whatever the user just said, but remind them that a human agent will be with them shortly. Keep it brief and polite (1 or 2 sentences max)."
                
                openai_messages = [{"role": "system", "content": wait_prompt}]
                openai_messages.extend([{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[-3:] if not m.get("is_slack")])
                
                try:
                    response = openai_client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=openai_messages,
                        stream=False,
                    )
                    full_response = response.choices[0].message.content
                    st.session_state.messages.append({"role": "assistant", "content": full_response, "is_slack": False})
                    st.rerun()
                except Exception as e:
                    st.error(f"OpenAI Error: {e}")

    # STATE: IN_HUMAN_CHAT
    elif st.session_state.escalation_state == "IN_HUMAN_CHAT":
        try:
            slack_client.chat_postMessage(
                channel=SLACK_CHANNEL_ID,
                thread_ts=st.session_state.slack_thread_ts,
                text=f"*User*: {prompt}"
            )
        except SlackApiError as e:
            st.error(f"Error sending message to Slack: {e.response['error']}")
