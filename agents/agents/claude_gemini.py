from agents.agents.base import BaseAgent
from agents.shared.llm_clients import call_gemini_with_retry, parse_qwen3vl_response
from agents.shared.prompts import GEMINI_SYSTEM_PROMPT_SINGLE_STEP, TOOL_DEFINITIONS
from agents.agents.claude import ClaudeAgent
from PIL import Image
import json
import os
import pickle
from copy import deepcopy
from io import BytesIO
import base64
import numpy as np


# python -m agents.evaluation.run_single --env_dir benchmarks/cua_world/environments/gimp_env_all_fast --task saturation_increase --agent 'ClaudeDatabricksAgent' --agent_args "{\"model\":\"databricks/claude-4-5-sonnet\", \"exp_name\":\"claude-4-5-sonnet\", \"task_name\": \"saturation_increase\"}"

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.bool_):
            return bool(obj)
        # Let the base class default method raise the TypeError for other unhandled types
        return json.JSONEncoder.default(self, obj)


class Gemini3Agent(ClaudeAgent):
    """
    Claude Databricks agent using Claude Databricks models via OpenAI-compatible API.
    Maintains a history-based prompting approach with image preprocessing.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.system_prompt = GEMINI_SYSTEM_PROMPT_SINGLE_STEP
        self.system_prompt = self.system_prompt.replace('<<TOOL_DEFINITIONS>>', json.dumps(TOOL_DEFINITIONS))
        self.execution_context = None
        self._tool_call_feedback = None
        self._pending_action = None
        self.raw_responses = []

    def set_execution_context(self, context):
        """Set temporary runner-owned guidance for the next policy turns."""
        self.execution_context = context

    def _action_feedback(self, action_outputs):
        """Pair environment results with submitted actions, rather than model plans."""
        pending = getattr(self, '_pending_action', None)
        self._pending_action = None
        results = []
        matched = False
        for output in action_outputs:
            failed = bool(output.get('is_error') or output.get('error')) or any(
                output.get(key) not in (None, 0)
                for key in ('exit_code', 'return_code', 'returncode')
            )
            result = {key: output[key] for key in (
                'tool_id', 'action', 'output', 'is_error', 'error',
                'exit_code', 'return_code', 'returncode',
            ) if key in output}
            if output.get('action') == 'screenshot' and not failed:
                result['output'] = 'Screenshot captured; see the current observation.'
            if pending is not None and output.get('tool_id') == pending['tool_id']:
                matched = True
                result['submitted_actions'] = pending['actions']
                result['status'] = 'execution_error' if failed else 'executed'
            else:
                result['status'] = 'unmatched_execution_result'
            results.append(result)
        if pending is not None and not matched:
            results.append({
                'tool_id': pending['tool_id'],
                'submitted_actions': pending['actions'],
                'status': 'execution_not_confirmed',
            })
        if results:
            return ('Previous action execution feedback (skipped tool calls were not executed):\n'
                    + json.dumps(results, default=str))
        return None

    def _observation_content(self, processed_image_b64, action_outputs=()):
        content = [{
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{processed_image_b64}"
            }
        }]
        if self.execution_context is not None:
            content.insert(0, {"type": "text", "text": self.execution_context})
        if getattr(self, '_tool_call_feedback', None):
            content.insert(0, {"type": "text", "text": self._tool_call_feedback})
            self._tool_call_feedback = None
        action_feedback = self._action_feedback(action_outputs)
        if action_feedback:
            content.insert(0, {"type": "text", "text": action_feedback})
        return content

    @staticmethod
    def _assistant_text(content):
        if content is None:
            return ''
        if isinstance(content, list) and all(
            isinstance(part, dict) and part.get('type') == 'text'
            and isinstance(part.get('text'), str) for part in content
        ):
            return '\n'.join(part['text'] for part in content)
        if not isinstance(content, str):
            raise TypeError('Gemini assistant content must be text or text content parts')
        return content

    def finish(self, *args, **kwargs):
        super().finish(*args, **kwargs)
        for folder in (self.save_path, self.save_folder_custom):
            with open(os.path.join(folder, 'raw_responses.pkl'), 'wb') as handle:
                pickle.dump(self.raw_responses, handle)

    def process_image(self, image_path, resize_to = None):
        """
        Process an image for Qwen VL models with smart resize.
        Returns base64 encoded processed image.
        """
        image = Image.open(image_path)
        width, height = image.size
        
        if self.verbose:
            print(f"Original screen resolution: {width}x{height}")
        
        if resize_to is not None:
            image = image.resize((resize_to[0], resize_to[1]))
                
        # Convert to base64
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        processed_bytes = buffer.getvalue()
        
        return base64.b64encode(processed_bytes).decode("utf-8")


    def step(self, obs, action_outputs):
        """
        Execute one agent step.
        
        Args:
            obs: Current observation from environment
            action_outputs: Execution results for the previously submitted action
        
        Returns:
            List of action groups to execute
        """
        # Save current observation
        self.save_observation(obs)
        self.step_idx += 1
        
        if self.messages[0]['role']!='system':
            self.messages.insert(0, {"role": "system", "content": self.system_prompt})
        # Process image
        processed_image_b64 = self.process_image(obs['screen']['path'], resize_to = (1920, 1080))
        
        # Build messages
        observation_content = self._observation_content(processed_image_b64, action_outputs)
        self.messages.append({"content": observation_content, "role": "user"})

        self.messages[-1]['cache_control'] = {"type": "ephemeral"}

        # Save messages for debugging
        # self.save_messages(self.messages)
        # Call LLM
        # print(f"Calling LLM with temperature: {self.temperature}")
        response = call_gemini_with_retry(
            self.messages, 
            self.model, 
            1.0, 
            0.95,
            20,
            reasoning_effort='low',
            return_full_response=True
        )
        self.raw_responses.append(response)
        response_message = response.choices[0].message
        assistant_content = self._assistant_text(response_message.content)
        self.messages.append({'role': 'assistant', 'content': assistant_content})
        reasoning_content = getattr(response_message, 'reasoning_content', None)

        if reasoning_content is not None:
            response = '<think>' + reasoning_content + '</think>\n' + assistant_content
        else:
            response = assistant_content

        
        
        # Store response for history
        
        # Parse response using existing parse_owl_response function
        # The shared parser executes at most one GUI tool call per response.
        parsed_response = parse_qwen3vl_response(response, scale_dims = True, scale_dims_ratio = (1920/1000, 1080/1000))
        
        # Store responses for later dumping
        # self.all_model_responses.append(response)
        # self.all_parsed_responses.append(parsed_response)
        
        # Extract actions and metadata
        actions = parsed_response['actions']
        metadata = parsed_response['metadata']
        self._tool_call_feedback = metadata.get('tool_call_warning')
        if metadata.get('parse_error'):
            self._tool_call_feedback = metadata['conclusion']
        if self._tool_call_feedback:
            self._tool_call_feedback = self._tool_call_feedback.replace(
                'will execute', 'was selected for execution'
            )
        
        # Update history with conclusion
        # self.history.append(metadata['conclusion'])
        
        if self.verbose:
            print(f"Step {self.step_idx + 1}:")
            print(f"  Thought: {metadata['thought']}")
            print(f"  Conclusion: {metadata['conclusion']}")
            print(f"  Action Type: {metadata['action_type']}")
            print(f"  Actions: {actions}")
        
        # Check if terminal
        if metadata['is_terminal']:
            self.done = True
            return []
        
        # Check if wait action
        if metadata['wait_time'] is not None:
            actions = [{'action': 'wait', 'time': metadata['wait_time']}]
        
        # Regular actions (mouse, keyboard, etc.)
        group = {
            'tool_id': f'qwen3vl_step_{self.step_idx}',
            'actions': actions,
            'metadata': metadata
        }
        self._pending_action = {
            'tool_id': group['tool_id'], 'actions': deepcopy(actions),
        }
        return [group]
