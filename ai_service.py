"""
AI Service Module for BCBA Test Prep
Handles all Anthropic Claude API interactions
"""

import anthropic
import json
from datetime import datetime
from config_ai import ANTHROPIC_API_KEY, AI_MODEL, AI_MAX_TOKENS, AI_TEMPERATURE, SYSTEM_PROMPT, PROMPT_VERSION


class AIService:
    """Service class for interacting with Anthropic Claude API"""
    
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = AI_MODEL
        self.max_tokens = AI_MAX_TOKENS
        self.temperature = AI_TEMPERATURE
        self.system_prompt = SYSTEM_PROMPT
        
    def test_connection(self):
        """Test API connection with a simple request"""
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=100,
                messages=[
                    {"role": "user", "content": "Say 'API connection successful!' and nothing else."}
                ]
            )
            return {
                'success': True,
                'message': message.content[0].text,
                'tokens': {
                    'input': message.usage.input_tokens,
                    'output': message.usage.output_tokens
                }
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def generate_explanation(self, question_data, user_answer=None):
        """
        Generate comprehensive explanation for a question
        
        Args:
            question_data: dict with keys: question_text, answers (list of dicts with answer_letter, answer_text, is_correct)
            user_answer: letter of user's selected answer (optional)
        
        Returns:
            dict with explanation, tokens used, cost, etc.
        """
        # Build the prompt
        correct_answer = next(a for a in question_data['answers'] if a['is_correct'])
        
        options_text = "\n".join([
            f"{a['answer_letter']}: {a['answer_text']}"
            for a in question_data['answers']
        ])
        
        user_answer_text = f"\n\nUser's Selected Answer: {user_answer}" if user_answer else "\n\nUser's Selected Answer: not provided"
        
        prompt = f"""Here is a BCBA exam-style question:

Question: {question_data['question_text']}

Options:
{options_text}

Correct Answer: {correct_answer['answer_letter']}{user_answer_text}

Please provide:

1. A clear explanation of why the correct answer ({correct_answer['answer_letter']}) is right, tying it to ABA principles and real-world application.

2. For each incorrect option, explain why it is wrong (what misconception it represents or why it doesn't apply).

3. The primary BCBA Task List domain(s) this question assesses (e.g., Behavior Assessment, Skill Acquisition and Behavior Reduction, etc.) and any secondary domains.

4. Key vocabulary terms/concepts from the question and answers, with brief definitions in BCBA context.

Structure your response clearly with headings for readability. Be thorough but concise — aim for exam-level precision.

IMPORTANT: Return your response as valid JSON with this structure:
{{
  "correct_explanation": "explanation here",
  "incorrect_explanations": {{
    "A": "why A is wrong (if incorrect)",
    "B": "why B is wrong (if incorrect)",
    "C": "why C is wrong (if incorrect)",
    "D": "why D is wrong (if incorrect)"
  }},
  "task_list_domains": {{
    "primary": "domain name",
    "secondary": ["other domains"]
  }},
  "key_vocabulary": [
    {{
      "term": "term name",
      "definition": "definition in BCBA context",
      "task_list_reference": "G-14 or similar"
    }}
  ],
  "study_tip": "helpful study tip"
}}"""
        
        try:
            # Call Claude API
            message = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=self.system_prompt,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            # Extract response
            response_text = message.content[0].text
            
            # Try to parse as JSON
            try:
                # Find JSON in response (Claude sometimes adds text before/after)
                json_start = response_text.find('{')
                json_end = response_text.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    json_text = response_text[json_start:json_end]
                    explanation_data = json.loads(json_text)
                else:
                    # Fallback: return as plain text
                    explanation_data = {
                        'raw_response': response_text,
                        'correct_explanation': response_text,
                        'parsing_error': 'Could not find JSON in response'
                    }
            except json.JSONDecodeError as e:
                # Fallback: return as plain text
                explanation_data = {
                    'raw_response': response_text,
                    'correct_explanation': response_text,
                    'parsing_error': str(e)
                }
            
            # Calculate cost
            input_cost = (message.usage.input_tokens / 1_000_000) * 3.0  # $3 per million
            output_cost = (message.usage.output_tokens / 1_000_000) * 15.0  # $15 per million
            total_cost = input_cost + output_cost
            
            return {
                'success': True,
                'explanation': explanation_data,
                'metadata': {
                    'model': self.model,
                    'prompt_version': PROMPT_VERSION,
                    'generated_at': datetime.now().isoformat(),
                    'tokens': {
                        'input': message.usage.input_tokens,
                        'output': message.usage.output_tokens,
                        'total': message.usage.input_tokens + message.usage.output_tokens
                    },
                    'cost': {
                        'input': input_cost,
                        'output': output_cost,
                        'total': total_cost
                    }
                }
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'error_type': type(e).__name__
            }


def generate_question_from_wrong_answer(original_question_text, answers, wrong_answer_letter, 
                                       original_correct_letter, original_question_number=None):
    """
    Generate a new question where a previously wrong answer becomes correct
    
    Args:
        original_question_text: The original question text
        answers: List of answer dicts with 'answer_letter', 'answer_text', 'is_correct'
        wrong_answer_letter: The letter of the wrong answer to make correct
        original_correct_letter: The letter of the original correct answer
        original_question_number: Optional question number for reference
        
    Returns:
        dict with:
            - success: bool
            - question: dict with new question data
            - tokens: dict with usage
            - cost: dict with cost breakdown
            - error: str if failed
    """
    service = AIService()
    
    # Find the wrong answer text
    wrong_answer_text = None
    for ans in answers:
        if ans['answer_letter'] == wrong_answer_letter:
            wrong_answer_text = ans['answer_text']
            break
    
    if not wrong_answer_text:
        return {
            'success': False,
            'error': f'Could not find answer {wrong_answer_letter}'
        }
    
    # Build options text
    options_text = ""
    for ans in answers:
        options_text += f"{ans['answer_letter']}: {ans['answer_text']}\n"
    
    # Build user prompt
    user_prompt = f"""You are Dr. Elena Vargas, an expert BCBA exam question writer.

ORIGINAL QUESTION:
{original_question_text}

ORIGINAL OPTIONS:
{options_text}
ORIGINAL CORRECT ANSWER: {original_correct_letter}

TASK:
Create ONE new BCBA exam-style question where option {wrong_answer_letter} ("{wrong_answer_text}") becomes the CORRECT answer.

REQUIREMENTS:
1. Make a realistic scenario where "{wrong_answer_text}" is clearly the RIGHT answer
2. Include 3 plausible but incorrect distractors (not from original question)
3. Same difficulty level and style as BCBA exam questions
4. Test the same or closely related BCBA concept
5. Be distinct from the original question (different scenario/context)
6. Include a brief explanation of why the answer is correct

Respond in this EXACT JSON format:
{{
  "question_text": "Your new question here",
  "options": [
    {{"letter": "A", "text": "Option text", "is_correct": true or false}},
    {{"letter": "B", "text": "Option text", "is_correct": true or false}},
    {{"letter": "C", "text": "Option text", "is_correct": true or false}},
    {{"letter": "D", "text": "Option text", "is_correct": true or false}}
  ],
  "explanation": "Brief explanation of why the correct answer is right",
  "difficulty_level": 2
}}

IMPORTANT: One option MUST be "{wrong_answer_text}" (or very similar) and MUST be marked is_correct: true"""
    
    # Call AI
    try:
        response = service.client.messages.create(
            model=service.model,
            max_tokens=2000,
            system=service.system_prompt,
            messages=[{
                "role": "user",
                "content": user_prompt
            }]
        )
        
        # Extract response text
        response_text = response.content[0].text
        
        # Parse JSON response
        import json
        import re
        
        # Try to extract JSON from response
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            question_data = json.loads(json_match.group())
        else:
            question_data = json.loads(response_text)
        
        # Calculate costs
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        total_tokens = input_tokens + output_tokens
        
        input_cost = (input_tokens / 1_000_000) * 3.00
        output_cost = (output_tokens / 1_000_000) * 15.00
        total_cost = input_cost + output_cost
        
        return {
            'success': True,
            'question': question_data,
            'tokens': {
                'input': input_tokens,
                'output': output_tokens,
                'total': total_tokens
            },
            'cost': {
                'input': input_cost,
                'output': output_cost,
                'total': total_cost
            },
            'model': service.model,
            'original_question_number': original_question_number,
            'wrong_answer_letter': wrong_answer_letter
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


# Convenience function for quick testing
def test_ai_service():
    """Test the AI service with a simple connection check"""
    service = AIService()
    result = service.test_connection()
    
    if result['success']:
        print("[SUCCESS] API Connection Successful!")
        print(f"Response: {result['message']}")
        print(f"Tokens used: {result['tokens']['input']} input + {result['tokens']['output']} output")
    else:
        print("[FAILED] API Connection Failed!")
        print(f"Error: {result['error']}")
    
    return result


if __name__ == "__main__":
    # Run test when file is executed directly
    test_ai_service()

