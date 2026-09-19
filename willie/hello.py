"""A5 deploy-loop test: edit GREETING on the Mac, `make deploy`, see it in the log within 10 s."""
import logging

GREETING = "Hello, I am WILL-E!"

log = logging.getLogger("willie.hello")


async def run():
    log.info(GREETING)
