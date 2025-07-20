import threading
import logging
import time

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def start_hedge():
    try:
        import hedge
        hedge.schedule_monitors()
    except Exception as e:
        logging.error(f"Error running hedge: {e}")

def start_monitor():
    try:
        import monitor
        monitor.run_forever()
    except Exception as e:
        logging.error(f"Error running monitor: {e}")

if __name__ == "__main__":
    logging.info("🚀 Starting main scheduler for hedge and monitor modules...")

    t1 = threading.Thread(target=start_hedge)
    t2 = threading.Thread(target=start_monitor)

    t1.start()
    t2.start()

    t1.join()
    t2.join()

    logging.info("🔚 All schedulers exited.")
