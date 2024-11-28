import logging
import os
from datetime import datetime

def setup_logging():
    # Create timestamped log directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join('logs', f"{timestamp}")
    os.makedirs(log_dir, exist_ok=True)

    # Setup main logger
    main_logger = logging.getLogger('main')
    main_logger.setLevel(logging.INFO)
    main_handler = logging.FileHandler(os.path.join(log_dir, 'main.log'))
    main_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    main_handler.setFormatter(main_formatter)
    main_logger.addHandler(main_handler)

    # Setup model logger
    model_logger = logging.getLogger('model')
    model_logger.setLevel(logging.INFO)
    model_handler = logging.FileHandler(os.path.join(log_dir, 'model.log'))
    model_formatter = logging.Formatter('--------------------------%(asctime)s--------------------------\n%(message)s')
    model_handler.setFormatter(model_formatter)
    model_logger.addHandler(model_handler)

    return main_logger, model_logger