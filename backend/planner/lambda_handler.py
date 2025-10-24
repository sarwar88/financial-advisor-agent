"""
Financial Planner Orchestrator Lambda Handler
"""

import os
import json
import asyncio
import logging
from typing import Dict, Any

from agents import Agent, Runner, trace
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from litellm.exceptions import RateLimitError

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# Import database package
from src import Database

from templates import ORCHESTRATOR_INSTRUCTIONS
from agent import create_agent, handle_missing_instruments, load_portfolio_summary
from market import update_instrument_prices
from observability import observe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize database
db = Database()

@retry(
    retry=retry_if_exception_type(RateLimitError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    before_sleep=lambda retry_state: logger.info(f"Planner: Rate limit hit, retrying in {retry_state.next_action.sleep} seconds...")
)
async def run_orchestrator(job_id: str) -> None:
    """Run the orchestrator agent to coordinate portfolio analysis."""
    try:
        # Update job status to running
        db.jobs.update_status(job_id, 'running')
        
        # Handle missing instruments first (non-agent pre-processing)
        await asyncio.to_thread(handle_missing_instruments, job_id, db)

        # Update instrument prices after tagging
        logger.info("Planner: Updating instrument prices from market data")
        await asyncio.to_thread(update_instrument_prices, job_id, db)

        # Load portfolio summary (just statistics, not full data)
        portfolio_summary = await asyncio.to_thread(load_portfolio_summary, job_id, db)
        
        # Create agent with tools and context
        model, tools, task, context = create_agent(job_id, portfolio_summary, db)
        
        # Run the orchestrator
        with trace("Planner Orchestrator"):
            from agent import PlannerContext
            agent = Agent[PlannerContext](
                name="Financial Planner",
                instructions=ORCHESTRATOR_INSTRUCTIONS,
                model=model,
                tools=tools
            )
            
            result = await Runner.run(
                agent,
                input=task,
                context=context,
                max_turns=20
            )
            
            # Mark job as completed after all agents finish
            db.jobs.update_status(job_id, "completed")
            logger.info(f"Planner: Job {job_id} completed successfully")
            
    except Exception as e:
        logger.error(f"Planner: Error in orchestration: {e}", exc_info=True)
        db.jobs.update_status(job_id, 'failed', error_message=str(e))
        raise

def lambda_handler(event, context):
    """
    Lambda handler for SQS-triggered orchestration.

    Expected event from SQS:
    {
        "Records": [
            {
                "body": "job_id"
            }
        ]
    }
    """
    # Wrap entire handler with observability context
    with observe():
        try:
            logger.info(f"Planner Lambda invoked with event: {json.dumps(event)[:500]}")

            # Extract job_id from SQS message
            if 'Records' in event and len(event['Records']) > 0:
                # SQS message
                job_id = event['Records'][0]['body']
                if isinstance(job_id, str) and job_id.startswith('{'):
                    # Body might be JSON
                    try:
                        body = json.loads(job_id)
                        job_id = body.get('job_id', job_id)
                    except json.JSONDecodeError:
                        pass
            elif 'job_id' in event:
                # Direct invocation
                job_id = event['job_id']
            else:
                logger.error("No job_id found in event")
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'No job_id provided'})
                }

            logger.info(f"Planner: Starting orchestration for job {job_id}")

            # Run the orchestrator
            asyncio.run(run_orchestrator(job_id))

            return {
                'statusCode': 200,
                'body': json.dumps({
                    'success': True,
                    'message': f'Analysis completed for job {job_id}'
                })
            }

        except Exception as e:
            logger.error(f"Planner: Error in lambda handler: {e}", exc_info=True)
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'success': False,
                    'error': str(e)
                })
            }

# For local testing
if __name__ == "__main__":
    # Create a test job first
    test_user_id = "test_user"
    
    # Create test job
    from src.schemas import JobCreate
    
    job_create = JobCreate(
        clerk_user_id=test_user_id,
        job_type='portfolio_analysis',
        request_payload={
            'analysis_type': 'comprehensive',
            'test': True
        }
    )
    
    job = db.jobs.create(job_create)
    job_id = job['id']
    
    print(f"Created test job: {job_id}")
    
    # Test the handler
    test_event = {
        'job_id': job_id
    }
    
    result = lambda_handler(test_event, None)
    print(json.dumps(result, indent=2))