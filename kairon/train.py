import asyncio
import os
import tempfile
from contextlib import ExitStack
from typing import Text, Optional, Dict
from urllib.parse import urljoin

import elasticapm
from loguru import logger as logging
from rasa.api import train
from rasa.model_training import DEFAULT_MODELS_PATH
from rasa.model_training import _train_async_internal, handle_domain_if_not_exists
from rasa.shared.constants import DEFAULT_CONFIG_PATH, DEFAULT_DATA_PATH, DEFAULT_DOMAIN_PATH
from rasa.shared.importers.importer import TrainingDataImporter
from rasa.utils.common import TempDirectoryPath

from kairon.chat.agent.agent import KaironAgent
from kairon.exceptions import AppException
from kairon.shared.account.processor import AccountProcessor
from kairon.shared.data.constant import EVENT_STATUS
from kairon.shared.data.importer import MongoDataImporter
from kairon.shared.data.model_processor import ModelProcessor
from kairon.shared.data.processor import MongoProcessor
from kairon.shared.llm.factory import LLMFactory
from kairon.shared.metering.constants import MetricType
from kairon.shared.metering.metering_processor import MeteringProcessor
from kairon.shared.utils import Utility


async def train_model(
        data_importer: TrainingDataImporter,
        output_path: Text,
        force_training: bool = False,
        fixed_model_name: Optional[Text] = None,
        persist_nlu_training_data: bool = False,
        additional_arguments: Optional[Dict] = None,
):
    """
    trains the bot, overridden the function from rasa

    :param data_importer: TrainingDataImporter object
    :param output_path: model output path
    :param force_training: w
    :param fixed_model_name:
    :param persist_nlu_training_data:
    :param additional_arguments:
    :return: model path
    """
    with ExitStack() as stack:
        train_path = stack.enter_context(TempDirectoryPath(tempfile.mkdtemp()))

        domain = await data_importer.get_domain()
        if domain.is_empty():
            return await handle_domain_if_not_exists(
                data_importer, output_path, fixed_model_name
            )

        return await _train_async_internal(
            data_importer,
            train_path,
            output_path,
            force_training,
            fixed_model_name,
            persist_nlu_training_data,
            additional_arguments,
        )


async def train_model_from_mongo(
        bot: str,
        force_training: bool = False,
        fixed_model_name: Optional[Text] = None,
        persist_nlu_training_data: bool = False,
        additional_arguments: Optional[Dict] = None,
):
    """
    trains the bot from loading the data from mongo

    :param bot:
    :param force_training:
    :param fixed_model_name:
    :param persist_nlu_training_data:
    :param additional_arguments:
    :return: model path

    :todo loading data directly from mongo, bot is not able to identify stories properly
    """
    data_importer = MongoDataImporter(bot)
    output = os.path.join(DEFAULT_MODELS_PATH, bot)
    return await train_model(
        data_importer,
        output,
        force_training,
        fixed_model_name,
        persist_nlu_training_data,
        additional_arguments,
    )


def train_model_for_bot(bot: str):
    """
    loads bot data from mongo into individual files for training

    :param bot: bot id
    :return: model path

    """
    processor = MongoProcessor()
    nlu = processor.load_nlu(bot)
    if not nlu.training_examples:
        raise AppException("Training data does not exists!")
    try:
        domain = processor.load_domain(bot)
        stories = processor.load_stories(bot)
        multiflow_stories = processor.load_linear_flows_from_multiflow_stories(bot)
        stories = stories.merge(multiflow_stories[0])
        config = processor.load_config(bot)
        rules = processor.get_rules_for_training(bot)
        rules = rules.merge(multiflow_stories[1])

        output = os.path.join(DEFAULT_MODELS_PATH, bot)

        directory = Utility.write_training_data(
            nlu,
            domain,
            config,
            stories,
            rules
        )

        if not os.path.exists(output):
            os.mkdir(output)
        model = train(
            domain=os.path.join(directory, DEFAULT_DOMAIN_PATH),
            config=os.path.join(directory, DEFAULT_CONFIG_PATH),
            training_files=os.path.join(directory, DEFAULT_DATA_PATH),
            output=directory,
            core_additional_arguments={"augmentation_factor": 100},
            force_training=True
        ).model

        KaironAgent.load(model_path=model)

        model_file = Utility.copy_model_file_to_directory(model, output)
        model = os.path.join(output, model_file)
        Utility.move_old_models(output, model)
        Utility.delete_directory(directory)
        Utility.delete_models(bot)
        del processor
        del nlu
        del domain
        del stories
        del rules
        del multiflow_stories
        del config
    except Exception as e:
        logging.exception(e)
        raise AppException(e)

    return model


def start_training(bot: str, user: str, token: str = None):
    """
    prevents training of the bot,
    if the training session is in progress otherwise start training

    :param reload: whether to reload model in the cache
    :param bot: bot id
    :param token: JWT token for remote model reload
    :param user: user id
    :return: model path
    """
    exception = None
    model_file = None
    training_status = None
    apm_client = None
    processor = MongoProcessor()
    try:
        apm_client = Utility.initiate_fastapi_apm_client()
        if apm_client:
            elasticapm.instrument()
            apm_client.begin_transaction(transaction_type="script")
        ModelProcessor.set_training_status(bot=bot, user=user, status=EVENT_STATUS.INPROGRESS.value)
        settings = processor.get_bot_settings(bot, user)
        settings = settings.to_mongo().to_dict()
        if settings["llm_settings"]['enable_faq']:
            llm = LLMFactory.get_instance("faq")(bot, settings["llm_settings"])
            faqs = asyncio.run(llm.train())
            account = AccountProcessor.get_bot(bot)['account']
            MeteringProcessor.add_metrics(bot=bot, metric_type=MetricType.faq_training.value, account=account, **faqs)
        agent_url = Utility.environment['model']['agent'].get('url')
        model_file = train_model_for_bot(bot)
        training_status = EVENT_STATUS.DONE.value
        if agent_url:
            if token:
                Utility.http_request('get', urljoin(agent_url, f"/api/bot/{bot}/reload"), token, user)
    except Exception as e:
        logging.exception(e)
        training_status = EVENT_STATUS.FAIL.value
        exception = str(e)
    finally:
        if apm_client:
            apm_client.end_transaction(name=__name__, result="success")
        ModelProcessor.set_training_status(
            bot=bot,
            user=user,
            status=training_status,
            model_path=model_file,
            exception=exception,
        )
    return model_file
