# functions/handlers/vertex/task/__init__.py
import asyncio
import traceback
import json
import uuid
from google.cloud import storage

from firebase_admin import firestore
from common.core import db, logger
from common.adk_helpers import instantiate_adk_agent_from_config
from google.genai.types import Content, Part
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.memory import InMemoryMemoryService
# CORRECTED IMPORT: Use agent_engines to get a deployed engine
from vertexai import agent_engines
import httpx
from a2a.types import Message as A2AMessage, TextPart

# --- Message History and Prompt Construction ---

async def get_full_message_history(chat_id: str, leaf_message_id: str | None) -> list[dict]:
    """Reconstructs the conversation history leading up to a specific message."""
    logger.info(f"[TaskExecutor] Fetching full message history for chat {chat_id} starting from leaf message {leaf_message_id}.")
    if not leaf_message_id:
        logger.info("[TaskExecutor] Leaf message ID is null, returning empty history.")
        return []
    messages = {}
    messages_collection = db.collection("chats").document(chat_id).collection("messages")
    # Fetch all messages in the chat once for efficiency
    for doc in messages_collection.stream():
        doc_data = doc.to_dict()
        messages[doc.id] = doc_data

    logger.info(f"[TaskExecutor] Retrieved {len(messages)} messages from Firestore for chat {chat_id}.")
    history = []
    current_id = leaf_message_id
    while current_id and current_id in messages:
        message = messages[current_id]
        history.insert(0, message)
        current_id = message.get("parentMessageId")
    logger.info(f"[TaskExecutor] Full history reconstructed with {len(history)} messages.")
    return history

async def _build_adk_content_from_history(
        conversation_history: list[dict]
) -> tuple[Content, int]:
    """
    Constructs a multi-part ADK Content object from the entire conversation history.
    This content object represents the full context/prompt for the ADK run.
    """
    adk_parts = []
    total_char_count = 0
    logger.info(f"[TaskExecutor] Building ADK content from {len(conversation_history)} history messages.")

    # The prompt is constructed from the entire conversation history.
    # To preserve turn structure within a single Content object, we process each
    # message and its parts from the history.
    for message in conversation_history:
        participant = message.get("participant", "")
        # Determine role for context. "assistant" becomes "model" for the LLM.
        role = "model" if participant.startswith("assistant:") else "user"

        # Aggregate all text from the current message into a single string.
        message_texts = [p.get("text", "") for p in message.get("parts", []) if "text" in p]
        if message_texts:
            full_text = "\n".join(message_texts).strip()
            if full_text:
                # Prepend the role to the text to distinguish turns, as we are flattening history.
                adk_parts.append(Part.from_text(text=f"{role}: {full_text}"))
                total_char_count += len(full_text)
                logger.info(f"Added text from '{role}' to ADK prompt.")

        # Process file parts separately. They will be associated with the flattened prompt.
        for part_data in message.get("parts", []):
            if "file_data" in part_data:
                file_info = part_data.get("file_data", {})
                if file_info is None:
                    file_info = {}
                uri = file_info.get("file_uri")
                mime_type = file_info.get("mime_type")

                if uri and mime_type:
                    # If the part is an image, download its bytes from GCS.
                    if mime_type.startswith("image/"):
                        try:
                            if not uri.startswith("gs://"):
                                raise ValueError(f"Unsupported URI scheme for image download: {uri}")
                            bucket_name = uri.split('/')[2]
                            blob_name = '/'.join(uri.split('/')[3:])
                            storage_client = storage.Client()
                            bucket = storage_client.bucket(bucket_name)
                            blob = bucket.blob(blob_name)
                            image_bytes = blob.download_as_bytes()
                            adk_parts.append(Part.from_bytes(data=image_bytes, mime_type=mime_type))
                            logger.info(f"Successfully downloaded image from {uri} to include in ADK prompt.")
                        except Exception as e:
                            logger.error(f"Failed to download image from GCS URI {uri} for ADK prompt: {e}")
                            adk_parts.append(Part.from_text(text=f"[{role} Error: Could not load image from {uri}]"))
                    elif mime_type.startswith("text/"):
                        try:
                            if not uri.startswith("gs://"):
                                raise ValueError(f"Unsupported URI scheme for text download: {uri}")
                            bucket_name = uri.split('/')[2]
                            blob_name = '/'.join(uri.split('/')[3:])
                            storage_client = storage.Client()
                            bucket = storage_client.bucket(bucket_name)
                            blob = bucket.blob(blob_name)
                            text_content = blob.download_as_text()
                            adk_parts.append(Part.from_text(text=f"{role} uploaded file '{blob_name}':\n{text_content}"))
                            logger.info(f"Successfully downloaded text from {uri} to include in ADK prompt.")
                        except Exception as e:
                            logger.error(f"Failed to download text from GCS URI {uri} for ADK prompt: {e}")
                            adk_parts.append(Part.from_text(text=f"[{role} Error: Could not load text from {uri}]"))
                    else:
                        # For other file types (like text from PDF), from_uri is appropriate.
                        logger.warn(f"[_build_adk_content_from_history] Throwing a hail mary- file_uris don't usually work...  {mime_type} isn't handled yet.")
                        adk_parts.append(Part.from_uri(file_uri=uri, mime_type=mime_type))

    if not adk_parts:
        logger.warn("No message parts were created. Adding an empty text part to avoid ADK error.")
        adk_parts.append(Part.from_text(text=""))

    logger.info(f"[TaskExecutor] Built {len(adk_parts)} ADK parts from {len(conversation_history)} messages.")
    # The entire history is flattened into a single 'user' turn. This is necessary
    # given the stateless nature of the task execution.
    return Content(role="user", parts=adk_parts), total_char_count

# --- Agent/Model Execution Logic ---

async def _run_adk_agent(local_adk_agent, adk_content_for_run, adk_user_id, assistant_message_id, events_collection_ref):
    """Runs a locally instantiated ADK agent (typically for an API-based model)."""
    from google.adk.artifacts import InMemoryArtifactService
    runner = Runner(
        agent=local_adk_agent,
        app_name=local_adk_agent.name,
        session_service=InMemorySessionService(),
        artifact_service=InMemoryArtifactService(),
        memory_service=InMemoryMemoryService()
    )
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id=adk_user_id)

    errors = []
    all_events = []
    try:
        # Step 1: Collect all events from the runner
        async for event_obj in runner.run_async(
                user_id=adk_user_id,
                session_id=session.id,
                new_message=adk_content_for_run
        ):
            all_events.append(event_obj.model_dump())
            #logger.info(f"[_run_adk_agent] Event collected: {event_obj.model_dump()}")
    except Exception as e_run:
        logger.error(f"Error during ADK agent run for '{local_adk_agent.name}': {e_run}\n{traceback.format_exc()}")
        errors.append(f"Agent/Model run failed: {str(e_run)}")

    #logger.info(f"[_run_adk_agent] Collected {len(all_events)} events from the ADK agent run.")
    # Step 2: Write all collected events to Firestore in a batch for efficiency
    batch = db.batch()
    for index, event_dict in enumerate(all_events):
        event_doc_ref = events_collection_ref.document()
        event_with_meta = {**event_dict, "eventIndex": index, "timestamp": firestore.SERVER_TIMESTAMP}
        batch.set(event_doc_ref, event_with_meta)
    if all_events:
        batch.commit()


    # Step 3: Find the final response from the collected events
    final_parts = []
    final_model_response_event = next(
        (event for event in reversed(all_events) if
           event.get('content', {}).get('role') == 'model' and
           not event.get("partial", False) and
                     not any(part.get('function_call', None) in part for part in event.get('content', {}).get('parts', []))),
    None
    )

    if final_model_response_event:
        logger.info(f"[_run_adk_agent] Final model response event found: {final_model_response_event}")
        content = final_model_response_event.get("content", {})
        if content and content.get("parts"):
            final_parts = content.get("parts")
    else:
        logger.warn("[_run_adk_agent] No final model response event found in the collected events.")

    return {"finalParts": final_parts, "errorDetails": errors}

async def _run_vertex_agent(resource_name, adk_content_for_run, adk_user_id, assistant_message_id, events_collection_ref):
    """Runs a deployed Vertex AI Reasoning Engine."""
    logger.info(f"Running deployed Vertex agent: {resource_name}")
    remote_app = agent_engines.get(resource_name)

    all_events = []
    errors = []
    try:
        # The deployed `stream_query` endpoint currently accepts a simple string `message`.
        # We must serialize our rich Content object into text for it.
        # This is a known limitation that means images in context are not passed to deployed agents.
        message_text_for_vertex = "\n".join([p.text for p in adk_content_for_run.parts if hasattr(p, 'text') and p.text])
        if not any(p.text for p in adk_content_for_run.parts if hasattr(p, 'text')):
            image_count = sum(1 for p in adk_content_for_run.parts if hasattr(p, 'file_data'))
            if image_count > 0:
                message_text_for_vertex = f"[Image Content Provided ({image_count})]"

        # Step 1: Collect all events from the runner
        for event_obj in remote_app.stream_query(
                message=message_text_for_vertex,
                user_id=adk_user_id,
                # session_id is now managed by the VertexAiSessionService within the remote_app context
        ):
            if hasattr(event_obj, 'model_dump'): event_dict = event_obj.model_dump()
            else: event_dict = event_obj

            all_events.append(event_dict)
        logger.info(f"[_run_vertex_agent] Collected {len(all_events)} events from the Vertex agent run.")

    except Exception as e:
        error_message = f"Vertex run failed: {str(e)}"
        errors.append(error_message)
        logger.error(f"Error during Vertex engine run: {e}", exc_info=True)


    # Step 2: Write all collected events to Firestore in a batch
    batch = db.batch()
    for index, event_dict in enumerate(all_events):
        event_doc_ref = events_collection_ref.document()
        logger.info(f"[_run_vertex_agent] Writing event {index} to Firestore: {event_dict}")
        event_with_meta = {**event_dict, "eventIndex": index, "timestamp": firestore.SERVER_TIMESTAMP}
        batch.set(event_doc_ref, event_with_meta)
    if all_events:
        batch.commit()

    # Step 3: Find the final response from the collected events
    final_parts = []
    final_model_response_event = next(
        (event for event in reversed(all_events) if
           event.get('content', {}).get('role') == 'model' and
            not event.get("partial", False) and
         not any(part.get('function_call', None) in part for part in event.get('content', {}).get('parts', []))),
        None
    )
    if final_model_response_event:
        content = final_model_response_event.get("content", {})
        if content and content.get("parts"):
            final_parts = content.get("parts")

    return {"finalParts": final_parts, "errorDetails": errors}

async def _run_a2a_agent(participant_config, adk_content_for_run, assistant_message_id, events_collection_ref):
    """Runs an A2A agent (unary)."""
    endpoint_url = participant_config.get("endpointUrl")
    if not endpoint_url:
        raise ValueError("A2A agent config is missing 'endpointUrl'.")
    message_text_for_a2a = "".join([part.text for part in adk_content_for_run.parts if hasattr(part, 'text') and part.text])
    a2a_message = A2AMessage(messageId=str(uuid.uuid4()), role="user", parts=[TextPart(text=message_text_for_a2a)])
    rpc_endpoint_url = endpoint_url.rstrip('/')
    errors, final_parts = [], []
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            rpc_payload = {
                "jsonrpc": "2.0", "method": "message/send", "id": f"agentlab-send-{uuid.uuid4().hex}",
                "params": {"message": a2a_message.model_dump(exclude_none=True)}
            }
            response = await client.post(rpc_endpoint_url, json=rpc_payload)
            response.raise_for_status()
            rpc_response = response.json()
            task_result = rpc_response.get("result")
            if task_result:
                event_doc_ref = events_collection_ref.document()
                event_doc_ref.set({"type": "a2a_unary_task_result", "source_event": task_result, "eventIndex": 0, "timestamp": firestore.SERVER_TIMESTAMP})

                final_text = ""
                for artifact in task_result.get("artifacts", []):
                    for part in artifact.get("parts", []):
                        if part.get("text") or part.get("text-delta"):
                            final_text += part.get("text", "") or part.get("text-delta", "")
                if final_text:
                    final_parts.append({"text": final_text})

            elif rpc_response.get("error"):
                errors.append(f"A2A 'message/send' error: {rpc_response['error']}")
        except Exception as e:
            logger.error(f"Failed to communicate with A2A agent: {e}\n{traceback.format_exc()}")
            errors.append(f"A2A communication failed: {e}")
    return {"finalParts": final_parts, "errorDetails": errors}

# --- Main Task Handler Logic ---

async def _execute_agent_run(
        chat_id: str, assistant_message_id: str, agent_id: str | None,
        model_id: str | None, adk_user_id: str
):
    """The core logic that runs in the background task."""
    logger.info(f"[TaskExecutor] Starting execution for message {assistant_message_id} in chat {chat_id}.")
    messages_collection_ref = db.collection("chats").document(chat_id).collection("messages")
    assistant_message_ref = messages_collection_ref.document(assistant_message_id)
    events_collection_ref = assistant_message_ref.collection("events")

    assistant_message_snap = assistant_message_ref.get() # .get() is synchronous in python-firestore
    if not assistant_message_snap.exists:
        raise ValueError(f"Assistant message {assistant_message_id} not found.")

    parent_message_id = assistant_message_snap.to_dict().get("parentMessageId")

    conversation_history = await get_full_message_history(chat_id, parent_message_id)
    logger.info(f"[TaskExecutor] Retrieved conversation_history: {conversation_history}")
    logger.info(f"[TaskExecutor] Full conversation history for message {assistant_message_id} retrieved with {len(conversation_history)} messages.")
    adk_content_for_run, char_count = await _build_adk_content_from_history(
        conversation_history
    )
    logger.info(f"[TaskExecutor] ADK content built with: {adk_content_for_run}")
    assistant_message_ref.update({"inputCharacterCount": char_count})

    participant_ref = db.collection("agents").document(agent_id) if agent_id else db.collection("models").document(model_id)
    participant_snap = participant_ref.get()
    if not participant_snap.exists: raise ValueError(f"Participant config not found for ID: {agent_id or model_id}")
    participant_config = participant_snap.to_dict()

    agent_platform = participant_config.get("platform")
    if agent_id and agent_platform == 'a2a':
        return await _run_a2a_agent(participant_config, adk_content_for_run, assistant_message_id, events_collection_ref)
    elif agent_id and agent_platform == 'google_vertex':
        logger.info("[TaskExecutor] Running Vertex AI agent.")
        resource_name = participant_config.get("vertexAiResourceName")
        if not resource_name or participant_config.get("deploymentStatus") != "deployed":
            raise ValueError(f"Agent {agent_id} is not successfully deployed.")
        logger.info("[TaskExecutor] Running Vertex AI agent.")
        return await _run_vertex_agent(resource_name, adk_content_for_run, adk_user_id, assistant_message_id, events_collection_ref)
    elif model_id:
        logger.info("[TaskExecutor] Running Model.")
        model_only_agent_config = {
            "name": f"ephemeral_model_run_{model_id[:6]}",
            "agentType": "Agent", "tools": [], "modelId": model_id,
        }
        local_adk_agent = await instantiate_adk_agent_from_config(model_only_agent_config)
        outputToReturn = await _run_adk_agent(local_adk_agent, adk_content_for_run, adk_user_id, assistant_message_id, events_collection_ref)
        logger.info(f"[TaskExecutor] Model run completed for message {assistant_message_id} with: {outputToReturn}")
        return outputToReturn
    logger.info("[TaskExecutor] Failed to run agent.")
    return {"finalParts": [], "errorDetails": [f"No valid execution path found for agentId: {agent_id}, modelId: {model_id}"]}

# --- Wrapper for Cloud Task ---

async def _run_agent_task_logic(data: dict):
    """Async logic for the task, with error handling."""
    chat_id = data.get("chatId")
    assistant_message_id = data.get("assistantMessageId")
    logger.info(f"[TaskHandler] Starting execution for message: {assistant_message_id}")
    assistant_message_ref = db.collection("chats").document(chat_id).collection("messages").document(assistant_message_id)
    try:
        assistant_message_ref.update({"status": "running"})
        final_state_data = await _execute_agent_run(
            chat_id=chat_id, assistant_message_id=assistant_message_id,
            agent_id=data.get("agentId"), model_id=data.get("modelId"),
            adk_user_id=data.get("adkUserId")
        )
        logger.info(f"[TaskHandler] Final state data for message {assistant_message_id}: {final_state_data}")
        final_update_payload = {
            "parts": final_state_data.get("finalParts", []),
            "status": "error" if final_state_data.get("errorDetails") else "completed",
            "errorDetails": final_state_data.get("errorDetails"),
            "completedTimestamp": firestore.SERVER_TIMESTAMP
        }
        assistant_message_ref.update(final_update_payload)
        logger.info(f"[TaskHandler] Message {assistant_message_id} completed with status: {final_update_payload['status']}")
    except Exception as e:
        error_msg = f"Unhandled exception in task handler for message {assistant_message_id}: {type(e).__name__} - {e}"
        logger.error(f"{error_msg}\n{traceback.format_exc()}")
        try:
            assistant_message_ref.update({
                "status": "error",
                "errorDetails": firestore.ArrayUnion([f"Task handler exception: {error_msg}"]),
                "completedTimestamp": firestore.SERVER_TIMESTAMP
            })
        except Exception as ee:
            logger.error(f"Failed to update error status for Firestore message {assistant_message_id}: {ee}", exc_info=True)

def run_agent_task_wrapper(data: dict):
    """Synchronous wrapper to be called by the Cloud Task entry point."""
    asyncio.run(_run_agent_task_logic(data))