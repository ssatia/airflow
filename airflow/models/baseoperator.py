#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Base operator for all operators."""
import abc
import copy
import functools
import logging
import sys
import warnings
from abc import ABCMeta, abstractmethod
from datetime import datetime, timedelta
from inspect import signature
from types import FunctionType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

import attr
import jinja2
import pendulum
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import NoResultFound

import airflow.templates
from airflow.compat.functools import cached_property
from airflow.configuration import conf
from airflow.exceptions import AirflowException, TaskDeferred
from airflow.lineage import apply_lineage, prepare_lineage
from airflow.models.base import Operator
from airflow.models.param import ParamsDict
from airflow.models.pool import Pool
from airflow.models.taskinstance import Context, TaskInstance, clear_task_instances
from airflow.models.taskmixin import TaskMixin
from airflow.models.xcom import XCOM_RETURN_KEY
from airflow.ti_deps.deps.base_ti_dep import BaseTIDep
from airflow.ti_deps.deps.not_in_retry_period_dep import NotInRetryPeriodDep
from airflow.ti_deps.deps.not_previously_skipped_dep import NotPreviouslySkippedDep
from airflow.ti_deps.deps.prev_dagrun_dep import PrevDagrunDep
from airflow.ti_deps.deps.trigger_rule_dep import TriggerRuleDep
from airflow.triggers.base import BaseTrigger
from airflow.utils import timezone
from airflow.utils.edgemodifier import EdgeModifier
from airflow.utils.helpers import validate_key
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.utils.operator_resources import Resources
from airflow.utils.session import provide_session
from airflow.utils.trigger_rule import TriggerRule
from airflow.utils.weight_rule import WeightRule

if TYPE_CHECKING:
    from airflow.models.dag import DAG
    from airflow.models.xcom_arg import XComArg
    from airflow.utils.task_group import TaskGroup

ScheduleInterval = Union[str, timedelta, relativedelta]

TaskStateChangeCallback = Callable[[Context], None]
TaskPreExecuteHook = Callable[[Context], None]
TaskPostExecuteHook = Callable[[Context, Any], None]

T = TypeVar('T', bound=FunctionType)


class BaseOperatorMeta(abc.ABCMeta):
    """Metaclass of BaseOperator."""

    @classmethod
    def _apply_defaults(cls, func: T) -> T:
        """
        Function decorator that Looks for an argument named "default_args", and
        fills the unspecified arguments from it.

        Since python2.* isn't clear about which arguments are missing when
        calling a function, and that this can be quite confusing with multi-level
        inheritance and argument defaults, this decorator also alerts with
        specific information about the missing arguments.
        """
        # Cache inspect.signature for the wrapper closure to avoid calling it
        # at every decorated invocation. This is separate sig_cache created
        # per decoration, i.e. each function decorated using apply_defaults will
        # have a different sig_cache.
        sig_cache = signature(func)
        non_optional_args = {
            name
            for (name, param) in sig_cache.parameters.items()
            if param.default == param.empty
            and param.name != 'self'
            and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        }

        class autostacklevel_warn:
            def __init__(self):
                self.warnings = __import__('warnings')

            def __getattr__(self, name):
                return getattr(self.warnings, name)

            def __dir__(self):
                return dir(self.warnings)

            def warn(self, message, category=None, stacklevel=1, source=None):
                self.warnings.warn(message, category, stacklevel + 2, source)

        if func.__globals__.get('warnings') is sys.modules['warnings']:
            # Yes, this is slightly hacky, but it _automatically_ sets the right
            # stacklevel parameter to `warnings.warn` to ignore the decorator. Now
            # that the decorator is applied automatically, this makes the needed
            # stacklevel parameter less confusing.
            func.__globals__['warnings'] = autostacklevel_warn()

        @functools.wraps(func)
        def apply_defaults(self, *args: Any, **kwargs: Any) -> Any:
            from airflow.models.dag import DagContext
            from airflow.utils.task_group import TaskGroupContext

            if len(args) > 0:
                raise AirflowException("Use keyword arguments when initializing operators")
            dag_args: Dict[str, Any] = {}
            dag_params: Dict[str, Any] = {}

            dag = kwargs.get('dag') or DagContext.get_current_dag()
            if dag:
                dag_args = copy.copy(dag.default_args) or {}
                dag_params = copy.deepcopy(dag.params) or {}
                task_group = TaskGroupContext.get_current_task_group(dag)
                if task_group:
                    dag_args.update(task_group.default_args)

            params = kwargs.get('params', {}) or {}
            dag_params.update(params)

            default_args = {}
            if 'default_args' in kwargs:
                default_args = kwargs['default_args']
                if 'params' in default_args:
                    dag_params.update(default_args['params'])
                    del default_args['params']

            dag_args.update(default_args)
            default_args = dag_args

            for arg in sig_cache.parameters:
                if arg not in kwargs and arg in default_args:
                    kwargs[arg] = default_args[arg]

            missing_args = list(non_optional_args - set(kwargs))
            if missing_args:
                msg = f"Argument {missing_args} is required"
                raise AirflowException(msg)

            if dag_params:
                kwargs['params'] = dag_params

            if default_args:
                kwargs['default_args'] = default_args

            if hasattr(self, '_hook_apply_defaults'):
                args, kwargs = self._hook_apply_defaults(*args, **kwargs)

            result = func(self, *args, **kwargs)

            # Here we set upstream task defined by XComArgs passed to template fields of the operator
            self.set_xcomargs_dependencies()

            # Mark instance as instantiated https://docs.python.org/3/tutorial/classes.html#private-variables
            self._BaseOperator__instantiated = True
            return result

        return cast(T, apply_defaults)

    def __new__(cls, name, bases, namespace, **kwargs):
        new_cls = super().__new__(cls, name, bases, namespace, **kwargs)
        new_cls.__init__ = cls._apply_defaults(new_cls.__init__)
        return new_cls


@functools.total_ordering
class BaseOperator(Operator, LoggingMixin, TaskMixin, metaclass=BaseOperatorMeta):
    """
    Abstract base class for all operators. Since operators create objects that
    become nodes in the dag, BaseOperator contains many recursive methods for
    dag crawling behavior. To derive this class, you are expected to override
    the constructor as well as the 'execute' method.

    Operators derived from this class should perform or trigger certain tasks
    synchronously (wait for completion). Example of operators could be an
    operator that runs a Pig job (PigOperator), a sensor operator that
    waits for a partition to land in Hive (HiveSensorOperator), or one that
    moves data from Hive to MySQL (Hive2MySqlOperator). Instances of these
    operators (tasks) target specific operations, running specific scripts,
    functions or data transfers.

    This class is abstract and shouldn't be instantiated. Instantiating a
    class derived from this one results in the creation of a task object,
    which ultimately becomes a node in DAG objects. Task dependencies should
    be set by using the set_upstream and/or set_downstream methods.

    :param task_id: a unique, meaningful id for the task
    :type task_id: str
    :param owner: the owner of the task. Using a meaningful description
        (e.g. user/person/team/role name) to clarify ownership is recommended.
    :type owner: str
    :param email: the 'to' email address(es) used in email alerts. This can be a
        single email or multiple ones. Multiple addresses can be specified as a
        comma or semi-colon separated string or by passing a list of strings.
    :type email: str or list[str]
    :param email_on_retry: Indicates whether email alerts should be sent when a
        task is retried
    :type email_on_retry: bool
    :param email_on_failure: Indicates whether email alerts should be sent when
        a task failed
    :type email_on_failure: bool
    :param retries: the number of retries that should be performed before
        failing the task
    :type retries: int
    :param retry_delay: delay between retries, can be set as ``timedelta`` or
        ``float`` seconds, which will be converted into ``timedelta``,
        the default is ``timedelta(seconds=300)``.
    :type retry_delay: datetime.timedelta or float
    :param retry_exponential_backoff: allow progressively longer waits between
        retries by using exponential backoff algorithm on retry delay (delay
        will be converted into seconds)
    :type retry_exponential_backoff: bool
    :param max_retry_delay: maximum delay interval between retries, can be set as
        ``timedelta`` or ``float`` seconds, which will be converted into ``timedelta``.
    :type max_retry_delay: datetime.timedelta or float
    :param start_date: The ``start_date`` for the task, determines
        the ``execution_date`` for the first task instance. The best practice
        is to have the start_date rounded
        to your DAG's ``schedule_interval``. Daily jobs have their start_date
        some day at 00:00:00, hourly jobs have their start_date at 00:00
        of a specific hour. Note that Airflow simply looks at the latest
        ``execution_date`` and adds the ``schedule_interval`` to determine
        the next ``execution_date``. It is also very important
        to note that different tasks' dependencies
        need to line up in time. If task A depends on task B and their
        start_date are offset in a way that their execution_date don't line
        up, A's dependencies will never be met. If you are looking to delay
        a task, for example running a daily task at 2AM, look into the
        ``TimeSensor`` and ``TimeDeltaSensor``. We advise against using
        dynamic ``start_date`` and recommend using fixed ones. Read the
        FAQ entry about start_date for more information.
    :type start_date: datetime.datetime
    :param end_date: if specified, the scheduler won't go beyond this date
    :type end_date: datetime.datetime
    :param depends_on_past: when set to true, task instances will run
        sequentially and only if the previous instance has succeeded or has been skipped.
        The task instance for the start_date is allowed to run.
    :type depends_on_past: bool
    :param wait_for_downstream: when set to true, an instance of task
        X will wait for tasks immediately downstream of the previous instance
        of task X to finish successfully or be skipped before it runs. This is useful if the
        different instances of a task X alter the same asset, and this asset
        is used by tasks downstream of task X. Note that depends_on_past
        is forced to True wherever wait_for_downstream is used. Also note that
        only tasks *immediately* downstream of the previous task instance are waited
        for; the statuses of any tasks further downstream are ignored.
    :type wait_for_downstream: bool
    :param dag: a reference to the dag the task is attached to (if any)
    :type dag: airflow.models.DAG
    :param priority_weight: priority weight of this task against other task.
        This allows the executor to trigger higher priority tasks before
        others when things get backed up. Set priority_weight as a higher
        number for more important tasks.
    :type priority_weight: int
    :param weight_rule: weighting method used for the effective total
        priority weight of the task. Options are:
        ``{ downstream | upstream | absolute }`` default is ``downstream``
        When set to ``downstream`` the effective weight of the task is the
        aggregate sum of all downstream descendants. As a result, upstream
        tasks will have higher weight and will be scheduled more aggressively
        when using positive weight values. This is useful when you have
        multiple dag run instances and desire to have all upstream tasks to
        complete for all runs before each dag can continue processing
        downstream tasks. When set to ``upstream`` the effective weight is the
        aggregate sum of all upstream ancestors. This is the opposite where
        downstream tasks have higher weight and will be scheduled more
        aggressively when using positive weight values. This is useful when you
        have multiple dag run instances and prefer to have each dag complete
        before starting upstream tasks of other dags.  When set to
        ``absolute``, the effective weight is the exact ``priority_weight``
        specified without additional weighting. You may want to do this when
        you know exactly what priority weight each task should have.
        Additionally, when set to ``absolute``, there is bonus effect of
        significantly speeding up the task creation process as for very large
        DAGs. Options can be set as string or using the constants defined in
        the static class ``airflow.utils.WeightRule``
    :type weight_rule: str
    :param queue: which queue to target when running this job. Not
        all executors implement queue management, the CeleryExecutor
        does support targeting specific queues.
    :type queue: str
    :param pool: the slot pool this task should run in, slot pools are a
        way to limit concurrency for certain tasks
    :type pool: str
    :param pool_slots: the number of pool slots this task should use (>= 1)
        Values less than 1 are not allowed.
    :type pool_slots: int
    :param sla: time by which the job is expected to succeed. Note that
        this represents the ``timedelta`` after the period is closed. For
        example if you set an SLA of 1 hour, the scheduler would send an email
        soon after 1:00AM on the ``2016-01-02`` if the ``2016-01-01`` instance
        has not succeeded yet.
        The scheduler pays special attention for jobs with an SLA and
        sends alert
        emails for SLA misses. SLA misses are also recorded in the database
        for future reference. All tasks that share the same SLA time
        get bundled in a single email, sent soon after that time. SLA
        notification are sent once and only once for each task instance.
    :type sla: datetime.timedelta
    :param execution_timeout: max time allowed for the execution of
        this task instance, if it goes beyond it will raise and fail.
    :type execution_timeout: datetime.timedelta
    :param on_failure_callback: a function to be called when a task instance
        of this task fails. a context dictionary is passed as a single
        parameter to this function. Context contains references to related
        objects to the task instance and is documented under the macros
        section of the API.
    :type on_failure_callback: TaskStateChangeCallback
    :param on_execute_callback: much like the ``on_failure_callback`` except
        that it is executed right before the task is executed.
    :type on_execute_callback: TaskStateChangeCallback
    :param on_retry_callback: much like the ``on_failure_callback`` except
        that it is executed when retries occur.
    :type on_retry_callback: TaskStateChangeCallback
    :param on_success_callback: much like the ``on_failure_callback`` except
        that it is executed when the task succeeds.
    :type on_success_callback: TaskStateChangeCallback
    :param pre_execute: a function to be called immediately before task
        execution, receiving a context dictionary; raising an exception will
        prevent the task from being executed.

        |experimental|
    :type pre_execute: TaskPreExecuteHook
    :param post_execute: a function to be called immediately after task
        execution, receiving a context dictionary and task result; raising an
        exception will prevent the task from succeeding.

        |experimental|
    :type post_execute: TaskPostExecuteHook
    :param trigger_rule: defines the rule by which dependencies are applied
        for the task to get triggered. Options are:
        ``{ all_success | all_failed | all_done | one_success |
        one_failed | none_failed | none_failed_min_one_success | none_skipped | always}``
        default is ``all_success``. Options can be set as string or
        using the constants defined in the static class
        ``airflow.utils.TriggerRule``
    :type trigger_rule: str
    :param resources: A map of resource parameter names (the argument names of the
        Resources constructor) to their values.
    :type resources: dict
    :param run_as_user: unix username to impersonate while running the task
    :type run_as_user: str
    :param max_active_tis_per_dag: When set, a task will be able to limit the concurrent
        runs across execution_dates.
    :type max_active_tis_per_dag: int
    :param executor_config: Additional task-level configuration parameters that are
        interpreted by a specific executor. Parameters are namespaced by the name of
        executor.

        **Example**: to run this task in a specific docker container through
        the KubernetesExecutor ::

            MyOperator(...,
                executor_config={
                    "KubernetesExecutor":
                        {"image": "myCustomDockerImage"}
                }
            )

    :type executor_config: dict
    :param do_xcom_push: if True, an XCom is pushed containing the Operator's
        result
    :type do_xcom_push: bool
    :param task_group: The TaskGroup to which the task should belong. This is typically provided when not
        using a TaskGroup as a context manager.
    :type task_group: airflow.utils.task_group.TaskGroup
    :param doc: Add documentation or notes to your Task objects that is visible in
        Task Instance details View in the Webserver
    :type doc: str
    :param doc_md: Add documentation (in Markdown format) or notes to your Task objects
        that is visible in Task Instance details View in the Webserver
    :type doc_md: str
    :param doc_rst: Add documentation (in RST format) or notes to your Task objects
        that is visible in Task Instance details View in the Webserver
    :type doc_rst: str
    :param doc_json: Add documentation (in JSON format) or notes to your Task objects
        that is visible in Task Instance details View in the Webserver
    :type doc_json: str
    :param doc_yaml: Add documentation (in YAML format) or notes to your Task objects
        that is visible in Task Instance details View in the Webserver
    :type doc_yaml: str
    """

    # For derived classes to define which fields will get jinjaified
    template_fields: Iterable[str] = ()
    # Defines which files extensions to look for in the templated fields
    template_ext: Iterable[str] = ()
    # Template field renderers indicating type of the field, for example sql, json, bash
    template_fields_renderers: Dict[str, str] = {}

    # Defines the color in the UI
    ui_color = '#fff'  # type: str
    ui_fgcolor = '#000'  # type: str

    pool = ""  # type: str

    # base list which includes all the attrs that don't need deep copy.
    _base_operator_shallow_copy_attrs: Tuple[str, ...] = (
        'user_defined_macros',
        'user_defined_filters',
        'params',
        '_log',
    )

    # each operator should override this class attr for shallow copy attrs.
    shallow_copy_attrs: Tuple[str, ...] = ()

    # Defines the operator level extra links
    operator_extra_links: Iterable['BaseOperatorLink'] = ()

    # The _serialized_fields are lazily loaded when get_serialized_fields() method is called
    __serialized_fields: Optional[FrozenSet[str]] = None

    _comps = {
        'task_id',
        'dag_id',
        'owner',
        'email',
        'email_on_retry',
        'retry_delay',
        'retry_exponential_backoff',
        'max_retry_delay',
        'start_date',
        'end_date',
        'depends_on_past',
        'wait_for_downstream',
        'priority_weight',
        'sla',
        'execution_timeout',
        'on_execute_callback',
        'on_failure_callback',
        'on_success_callback',
        'on_retry_callback',
        'do_xcom_push',
    }

    # Defines if the operator supports lineage without manual definitions
    supports_lineage = False

    # If True then the class constructor was called
    __instantiated = False

    # Set to True before calling execute method
    _lock_for_execution = False

    _dag: Optional["DAG"] = None

    # subdag parameter is only set for SubDagOperator.
    # Setting it to None by default as other Operators do not have that field
    subdag: Optional["DAG"] = None

    def __init__(
        self,
        task_id: str,
        owner: str = conf.get('operators', 'DEFAULT_OWNER'),
        email: Optional[Union[str, Iterable[str]]] = None,
        email_on_retry: bool = conf.getboolean('email', 'default_email_on_retry', fallback=True),
        email_on_failure: bool = conf.getboolean('email', 'default_email_on_failure', fallback=True),
        retries: Optional[int] = conf.getint('core', 'default_task_retries', fallback=0),
        retry_delay: Union[timedelta, float] = timedelta(seconds=300),
        retry_exponential_backoff: bool = False,
        max_retry_delay: Optional[Union[timedelta, float]] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        depends_on_past: bool = False,
        wait_for_downstream: bool = False,
        dag: Optional['DAG'] = None,
        params: Optional[Dict] = None,
        default_args: Optional[Dict] = None,
        priority_weight: int = 1,
        weight_rule: str = conf.get('core', 'default_task_weight_rule', fallback=WeightRule.DOWNSTREAM),
        queue: str = conf.get('operators', 'default_queue'),
        pool: Optional[str] = None,
        pool_slots: int = 1,
        sla: Optional[timedelta] = None,
        execution_timeout: Optional[timedelta] = None,
        on_execute_callback: Optional[TaskStateChangeCallback] = None,
        on_failure_callback: Optional[TaskStateChangeCallback] = None,
        on_success_callback: Optional[TaskStateChangeCallback] = None,
        on_retry_callback: Optional[TaskStateChangeCallback] = None,
        pre_execute: Optional[TaskPreExecuteHook] = None,
        post_execute: Optional[TaskPostExecuteHook] = None,
        trigger_rule: str = TriggerRule.ALL_SUCCESS,
        resources: Optional[Dict] = None,
        run_as_user: Optional[str] = None,
        task_concurrency: Optional[int] = None,
        max_active_tis_per_dag: Optional[int] = None,
        executor_config: Optional[Dict] = None,
        do_xcom_push: bool = True,
        inlets: Optional[Any] = None,
        outlets: Optional[Any] = None,
        task_group: Optional["TaskGroup"] = None,
        doc: Optional[str] = None,
        doc_md: Optional[str] = None,
        doc_json: Optional[str] = None,
        doc_yaml: Optional[str] = None,
        doc_rst: Optional[str] = None,
        **kwargs,
    ):
        from airflow.models.dag import DagContext
        from airflow.utils.task_group import TaskGroupContext

        super().__init__()
        if kwargs:
            if not conf.getboolean('operators', 'ALLOW_ILLEGAL_ARGUMENTS'):
                raise AirflowException(
                    f"Invalid arguments were passed to {self.__class__.__name__} (task_id: {task_id}). "
                    f"Invalid arguments were:\n**kwargs: {kwargs}",
                )
            warnings.warn(
                f'Invalid arguments were passed to {self.__class__.__name__} (task_id: {task_id}). '
                'Support for passing such arguments will be dropped in future. '
                f'Invalid arguments were:\n**kwargs: {kwargs}',
                category=PendingDeprecationWarning,
                stacklevel=3,
            )
        validate_key(task_id)
        self.task_id = task_id
        self.label = task_id
        task_group = task_group or TaskGroupContext.get_current_task_group(dag)
        if task_group:
            self.task_id = task_group.child_id(task_id)
            task_group.add(self)
        self.owner = owner
        self.email = email
        self.email_on_retry = email_on_retry
        self.email_on_failure = email_on_failure

        self.start_date = start_date
        if start_date and not isinstance(start_date, datetime):
            self.log.warning("start_date for %s isn't datetime.datetime", self)
        elif start_date:
            self.start_date = timezone.convert_to_utc(start_date)

        self.end_date = end_date
        if end_date:
            self.end_date = timezone.convert_to_utc(end_date)

        if trigger_rule == "dummy":
            warnings.warn(
                "dummy Trigger Rule is deprecated. Please use `TriggerRule.ALWAYS`.",
                DeprecationWarning,
                stacklevel=2,
            )
            trigger_rule = TriggerRule.ALWAYS

        if trigger_rule == "none_failed_or_skipped":
            warnings.warn(
                "none_failed_or_skipped Trigger Rule is deprecated. "
                "Please use `none_failed_min_one_success`.",
                DeprecationWarning,
                stacklevel=2,
            )
            trigger_rule = TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS

        if not TriggerRule.is_valid(trigger_rule):
            raise AirflowException(
                f"The trigger_rule must be one of {TriggerRule.all_triggers()},"
                f"'{dag.dag_id if dag else ''}.{task_id}'; received '{trigger_rule}'."
            )

        self.trigger_rule = trigger_rule
        self.depends_on_past = depends_on_past
        self.wait_for_downstream = wait_for_downstream
        if wait_for_downstream:
            self.depends_on_past = True

        if retries is not None and not isinstance(retries, int):
            try:
                parsed_retries = int(retries)
            except (TypeError, ValueError):
                raise AirflowException(f"'retries' type must be int, not {type(retries).__name__}")
            self.log.warning("Implicitly converting 'retries' for %s from %r to int", self, retries)
            retries = parsed_retries

        self.retries = retries
        self.queue = queue
        self.pool = Pool.DEFAULT_POOL_NAME if pool is None else pool
        self.pool_slots = pool_slots
        if self.pool_slots < 1:
            dag_str = f" in dag {dag.dag_id}" if dag else ""
            raise ValueError(f"pool slots for {self.task_id}{dag_str} cannot be less than 1")
        self.sla = sla
        self.execution_timeout = execution_timeout
        self.on_execute_callback = on_execute_callback
        self.on_failure_callback = on_failure_callback
        self.on_success_callback = on_success_callback
        self.on_retry_callback = on_retry_callback
        self._pre_execute_hook = pre_execute
        self._post_execute_hook = post_execute

        if isinstance(retry_delay, timedelta):
            self.retry_delay = retry_delay
        else:
            self.log.debug("Retry_delay isn't timedelta object, assuming secs")
            self.retry_delay = timedelta(seconds=retry_delay)
        self.retry_exponential_backoff = retry_exponential_backoff
        self.max_retry_delay = max_retry_delay
        if max_retry_delay:
            if isinstance(max_retry_delay, timedelta):
                self.max_retry_delay = max_retry_delay
            else:
                self.log.debug("max_retry_delay isn't a timedelta object, assuming secs")
                self.max_retry_delay = timedelta(seconds=max_retry_delay)

        # At execution_time this becomes a normal dict
        self.params: Union[ParamsDict, dict] = ParamsDict(params)
        if priority_weight is not None and not isinstance(priority_weight, int):
            raise AirflowException(
                f"`priority_weight` for task '{self.task_id}' only accepts integers, "
                f"received '{type(priority_weight)}'."
            )
        self.priority_weight = priority_weight
        if not WeightRule.is_valid(weight_rule):
            raise AirflowException(
                f"The weight_rule must be one of "
                f"{WeightRule.all_weight_rules},'{dag.dag_id if dag else ''}.{task_id}'; "
                f"received '{weight_rule}'."
            )
        self.weight_rule = weight_rule
        self.resources: Optional[Resources] = Resources(**resources) if resources else None
        self.run_as_user = run_as_user
        if task_concurrency and not max_active_tis_per_dag:
            # TODO: Remove in Airflow 3.0
            warnings.warn(
                "The 'task_concurrency' parameter is deprecated. Please use 'max_active_tis_per_dag'.",
                DeprecationWarning,
                stacklevel=2,
            )
            max_active_tis_per_dag = task_concurrency
        self.max_active_tis_per_dag = max_active_tis_per_dag
        self.executor_config = executor_config or {}
        self.do_xcom_push = do_xcom_push

        self.doc_md = doc_md
        self.doc_json = doc_json
        self.doc_yaml = doc_yaml
        self.doc_rst = doc_rst
        self.doc = doc

        # Private attributes
        self._upstream_task_ids: Set[str] = set()
        self._downstream_task_ids: Set[str] = set()

        dag = dag or DagContext.get_current_dag()
        if dag:
            self.dag = dag

        self._log = logging.getLogger("airflow.task.operators")

        # Lineage
        self.inlets: List = []
        self.outlets: List = []

        self._inlets: List = []
        self._outlets: List = []

        if inlets:
            self._inlets = (
                inlets
                if isinstance(inlets, list)
                else [
                    inlets,
                ]
            )

        if outlets:
            self._outlets = (
                outlets
                if isinstance(outlets, list)
                else [
                    outlets,
                ]
            )

    def __eq__(self, other):
        if type(self) is type(other):
            # Use getattr() instead of __dict__ as __dict__ doesn't return
            # correct values for properties.
            return all(getattr(self, c, None) == getattr(other, c, None) for c in self._comps)
        return False

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        hash_components = [type(self)]
        for component in self._comps:
            val = getattr(self, component, None)
            try:
                hash(val)
                hash_components.append(val)
            except TypeError:
                hash_components.append(repr(val))
        return hash(tuple(hash_components))

    # including lineage information
    def __or__(self, other):
        """
        Called for [This Operator] | [Operator], The inlets of other
        will be set to pickup the outlets from this operator. Other will
        be set as a downstream task of this operator.
        """
        if isinstance(other, BaseOperator):
            if not self._outlets and not self.supports_lineage:
                raise ValueError("No outlets defined for this operator")
            other.add_inlets([self.task_id])
            self.set_downstream(other)
        else:
            raise TypeError(f"Right hand side ({other}) is not an Operator")

        return self

    # /Composing Operators ---------------------------------------------

    def __gt__(self, other):
        """
        Called for [Operator] > [Outlet], so that if other is an attr annotated object
        it is set as an outlet of this Operator.
        """
        if not isinstance(other, Iterable):
            other = [other]

        for obj in other:
            if not attr.has(obj):
                raise TypeError(f"Left hand side ({obj}) is not an outlet")
        self.add_outlets(other)

        return self

    def __lt__(self, other):
        """
        Called for [Inlet] > [Operator] or [Operator] < [Inlet], so that if other is
        an attr annotated object it is set as an inlet to this operator
        """
        if not isinstance(other, Iterable):
            other = [other]

        for obj in other:
            if not attr.has(obj):
                raise TypeError(f"{obj} cannot be an inlet")
        self.add_inlets(other)

        return self

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        if self._lock_for_execution:
            # Skip any custom behaviour during execute
            return
        if self.__instantiated and key in self.template_fields:
            # Resolve upstreams set by assigning an XComArg after initializing
            # an operator, example:
            #   op = BashOperator()
            #   op.bash_command = "sleep 1"
            self.set_xcomargs_dependencies()

    def add_inlets(self, inlets: Iterable[Any]):
        """Sets inlets to this operator"""
        self._inlets.extend(inlets)

    def add_outlets(self, outlets: Iterable[Any]):
        """Defines the outlets of this operator"""
        self._outlets.extend(outlets)

    def get_inlet_defs(self):
        """:return: list of inlets defined for this operator"""
        return self._inlets

    def get_outlet_defs(self):
        """:return: list of outlets defined for this operator"""
        return self._outlets

    @property
    def dag(self) -> 'DAG':
        """Returns the Operator's DAG if set, otherwise raises an error"""
        if self._dag:
            return self._dag
        else:
            raise AirflowException(f'Operator {self} has not been assigned to a DAG yet')

    @dag.setter
    def dag(self, dag: Optional['DAG']):
        """
        Operators can be assigned to one DAG, one time. Repeat assignments to
        that same DAG are ok.
        """
        from airflow.models.dag import DAG

        if dag is None:
            self._dag = None
            return
        if not isinstance(dag, DAG):
            raise TypeError(f'Expected DAG; received {dag.__class__.__name__}')
        elif self.has_dag() and self.dag is not dag:
            raise AirflowException(f"The DAG assigned to {self} can not be changed.")
        elif self.task_id not in dag.task_dict:
            dag.add_task(self)
        elif self.task_id in dag.task_dict and dag.task_dict[self.task_id] is not self:
            dag.add_task(self)

        self._dag = dag

    def has_dag(self):
        """Returns True if the Operator has been assigned to a DAG."""
        return self._dag is not None

    @property
    def dag_id(self) -> str:
        """Returns dag id if it has one or an adhoc + owner"""
        if self.has_dag():
            return self.dag.dag_id
        else:
            return 'adhoc_' + self.owner

    deps: Iterable[BaseTIDep] = frozenset(
        {
            NotInRetryPeriodDep(),
            PrevDagrunDep(),
            TriggerRuleDep(),
            NotPreviouslySkippedDep(),
        }
    )
    """
    Returns the set of dependencies for the operator. These differ from execution
    context dependencies in that they are specific to tasks and can be
    extended/overridden by subclasses.
    """

    def prepare_for_execution(self) -> "BaseOperator":
        """
        Lock task for execution to disable custom action in __setattr__ and
        returns a copy of the task
        """
        other = copy.copy(self)
        other._lock_for_execution = True
        return other

    def set_xcomargs_dependencies(self) -> None:
        """
        Resolves upstream dependencies of a task. In this way passing an ``XComArg``
        as value for a template field will result in creating upstream relation between
        two tasks.

        **Example**: ::

            with DAG(...):
                generate_content = GenerateContentOperator(task_id="generate_content")
                send_email = EmailOperator(..., html_content=generate_content.output)

            # This is equivalent to
            with DAG(...):
                generate_content = GenerateContentOperator(task_id="generate_content")
                send_email = EmailOperator(
                    ..., html_content="{{ task_instance.xcom_pull('generate_content') }}"
                )
                generate_content >> send_email

        """
        from airflow.models.xcom_arg import XComArg

        def apply_set_upstream(arg: Any):
            if isinstance(arg, XComArg):
                self.set_upstream(arg.operator)
            elif isinstance(arg, (tuple, set, list)):
                for elem in arg:
                    apply_set_upstream(elem)
            elif isinstance(arg, dict):
                for elem in arg.values():
                    apply_set_upstream(elem)
            elif hasattr(arg, "template_fields"):
                for elem in arg.template_fields:
                    apply_set_upstream(elem)

        for field in self.template_fields:
            if hasattr(self, field):
                arg = getattr(self, field)
                apply_set_upstream(arg)

    @property
    def priority_weight_total(self) -> int:
        """
        Total priority weight for the task. It might include all upstream or downstream tasks.
        depending on the weight rule.

          - WeightRule.ABSOLUTE - only own weight
          - WeightRule.DOWNSTREAM - adds priority weight of all downstream tasks
          - WeightRule.UPSTREAM - adds priority weight of all upstream tasks

        """
        if self.weight_rule == WeightRule.ABSOLUTE:
            return self.priority_weight
        elif self.weight_rule == WeightRule.DOWNSTREAM:
            upstream = False
        elif self.weight_rule == WeightRule.UPSTREAM:
            upstream = True
        else:
            upstream = False

        if not self._dag:
            return self.priority_weight
        from airflow.models.dag import DAG

        dag: DAG = self._dag
        return self.priority_weight + sum(
            map(
                lambda task_id: dag.task_dict[task_id].priority_weight,
                self.get_flat_relative_ids(upstream=upstream),
            )
        )

    @cached_property
    def operator_extra_link_dict(self) -> Dict[str, Any]:
        """Returns dictionary of all extra links for the operator"""
        op_extra_links_from_plugin: Dict[str, Any] = {}
        from airflow import plugins_manager

        plugins_manager.initialize_extra_operators_links_plugins()
        if plugins_manager.operator_extra_links is None:
            raise AirflowException("Can't load operators")
        for ope in plugins_manager.operator_extra_links:
            if ope.operators and self.__class__ in ope.operators:
                op_extra_links_from_plugin.update({ope.name: ope})

        operator_extra_links_all = {link.name: link for link in self.operator_extra_links}
        # Extra links defined in Plugins overrides operator links defined in operator
        operator_extra_links_all.update(op_extra_links_from_plugin)

        return operator_extra_links_all

    @cached_property
    def global_operator_extra_link_dict(self) -> Dict[str, Any]:
        """Returns dictionary of all global extra links"""
        from airflow import plugins_manager

        plugins_manager.initialize_extra_operators_links_plugins()
        if plugins_manager.global_operator_extra_links is None:
            raise AirflowException("Can't load operators")
        return {link.name: link for link in plugins_manager.global_operator_extra_links}

    @prepare_lineage
    def pre_execute(self, context: Any):
        """This hook is triggered right before self.execute() is called."""
        if self._pre_execute_hook is not None:
            self._pre_execute_hook(context)

    def execute(self, context: Any):
        """
        This is the main method to derive when creating an operator.
        Context is the same dictionary used as when rendering jinja templates.

        Refer to get_template_context for more context.
        """
        raise NotImplementedError()

    @apply_lineage
    def post_execute(self, context: Any, result: Any = None):
        """
        This hook is triggered right after self.execute() is called.
        It is passed the execution context and any results returned by the
        operator.
        """
        if self._post_execute_hook is not None:
            self._post_execute_hook(context, result)

    def on_kill(self) -> None:
        """
        Override this method to cleanup subprocesses when a task instance
        gets killed. Any use of the threading, subprocess or multiprocessing
        module within an operator needs to be cleaned up or it will leave
        ghost processes behind.
        """

    def __deepcopy__(self, memo):
        """
        Hack sorting double chained task lists by task_id to avoid hitting
        max_depth on deepcopy operations.
        """
        sys.setrecursionlimit(5000)  # TODO fix this in a better way
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result

        shallow_copy = cls.shallow_copy_attrs + cls._base_operator_shallow_copy_attrs

        for k, v in self.__dict__.items():
            if k not in shallow_copy:
                setattr(result, k, copy.deepcopy(v, memo))
            else:
                setattr(result, k, copy.copy(v))
        return result

    def __getstate__(self):
        state = dict(self.__dict__)
        del state['_log']

        return state

    def __setstate__(self, state):
        self.__dict__ = state
        self._log = logging.getLogger("airflow.task.operators")

    def render_template_fields(self, context: Dict, jinja_env: Optional[jinja2.Environment] = None) -> None:
        """
        Template all attributes listed in template_fields. Note this operation is irreversible.

        :param context: Dict with values to apply on content
        :type context: dict
        :param jinja_env: Jinja environment
        :type jinja_env: jinja2.Environment
        """
        if not jinja_env:
            jinja_env = self.get_template_env()

        self._do_render_template_fields(self, self.template_fields, context, jinja_env, set())

    def _do_render_template_fields(
        self,
        parent: Any,
        template_fields: Iterable[str],
        context: Dict,
        jinja_env: jinja2.Environment,
        seen_oids: Set,
    ) -> None:
        for attr_name in template_fields:
            content = getattr(parent, attr_name)
            if content:
                rendered_content = self.render_template(content, context, jinja_env, seen_oids)
                setattr(parent, attr_name, rendered_content)

    def render_template(
        self,
        content: Any,
        context: Dict,
        jinja_env: Optional[jinja2.Environment] = None,
        seen_oids: Optional[Set] = None,
    ) -> Any:
        """
        Render a templated string. The content can be a collection holding multiple templated strings and will
        be templated recursively.

        :param content: Content to template. Only strings can be templated (may be inside collection).
        :type content: Any
        :param context: Dict with values to apply on templated content
        :type context: dict
        :param jinja_env: Jinja environment. Can be provided to avoid re-creating Jinja environments during
            recursion.
        :type jinja_env: jinja2.Environment
        :param seen_oids: template fields already rendered (to avoid RecursionError on circular dependencies)
        :type seen_oids: set
        :return: Templated content
        """
        if not jinja_env:
            jinja_env = self.get_template_env()

        # Imported here to avoid circular dependency
        from airflow.models.param import DagParam
        from airflow.models.xcom_arg import XComArg

        if isinstance(content, str):
            if any(content.endswith(ext) for ext in self.template_ext):
                # Content contains a filepath
                return jinja_env.get_template(content).render(**context)
            else:
                return jinja_env.from_string(content).render(**context)
        elif isinstance(content, (XComArg, DagParam)):
            return content.resolve(context)

        if isinstance(content, tuple):
            if type(content) is not tuple:
                # Special case for named tuples
                return content.__class__(
                    *(self.render_template(element, context, jinja_env) for element in content)
                )
            else:
                return tuple(self.render_template(element, context, jinja_env) for element in content)

        elif isinstance(content, list):
            return [self.render_template(element, context, jinja_env) for element in content]

        elif isinstance(content, dict):
            return {key: self.render_template(value, context, jinja_env) for key, value in content.items()}

        elif isinstance(content, set):
            return {self.render_template(element, context, jinja_env) for element in content}

        else:
            if seen_oids is None:
                seen_oids = set()
            self._render_nested_template_fields(content, context, jinja_env, seen_oids)
            return content

    def _render_nested_template_fields(
        self, content: Any, context: Dict, jinja_env: jinja2.Environment, seen_oids: Set
    ) -> None:
        if id(content) not in seen_oids:
            seen_oids.add(id(content))
            try:
                nested_template_fields = content.template_fields
            except AttributeError:
                # content has no inner template fields
                return

            self._do_render_template_fields(content, nested_template_fields, context, jinja_env, seen_oids)

    def get_template_env(self) -> jinja2.Environment:
        """Fetch a Jinja template environment from the DAG or instantiate empty environment if no DAG."""
        return (
            self.dag.get_template_env()
            if self.has_dag()
            else airflow.templates.SandboxedEnvironment(cache_size=0)
        )

    def prepare_template(self) -> None:
        """
        Hook that is triggered after the templated fields get replaced
        by their content. If you need your operator to alter the
        content of the file before the template is rendered,
        it should override this method to do so.
        """

    def resolve_template_files(self) -> None:
        """Getting the content of files for template_field / template_ext"""
        if self.template_ext:
            for field in self.template_fields:
                content = getattr(self, field, None)
                if content is None:
                    continue
                elif isinstance(content, str) and any(content.endswith(ext) for ext in self.template_ext):
                    env = self.get_template_env()
                    try:
                        setattr(self, field, env.loader.get_source(env, content)[0])
                    except Exception as e:
                        self.log.exception(e)
                elif isinstance(content, list):
                    env = self.dag.get_template_env()
                    for i, item in enumerate(content):
                        if isinstance(item, str) and any(item.endswith(ext) for ext in self.template_ext):
                            try:
                                content[i] = env.loader.get_source(env, item)[0]
                            except Exception as e:
                                self.log.exception(e)
        self.prepare_template()

    @property
    def upstream_list(self) -> List["BaseOperator"]:
        """@property: list of tasks directly upstream"""
        return [self.dag.get_task(tid) for tid in self._upstream_task_ids]

    @property
    def upstream_task_ids(self) -> Set[str]:
        """@property: set of ids of tasks directly upstream"""
        return self._upstream_task_ids

    @property
    def downstream_list(self) -> List["BaseOperator"]:
        """@property: list of tasks directly downstream"""
        return [self.dag.get_task(tid) for tid in self._downstream_task_ids]

    @property
    def downstream_task_ids(self) -> Set[str]:
        """@property: set of ids of tasks directly downstream"""
        return self._downstream_task_ids

    @provide_session
    def clear(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        upstream: bool = False,
        downstream: bool = False,
        session: Session = None,
    ):
        """
        Clears the state of task instances associated with the task, following
        the parameters specified.
        """
        qry = session.query(TaskInstance).filter(TaskInstance.dag_id == self.dag_id)

        if start_date:
            qry = qry.filter(TaskInstance.execution_date >= start_date)
        if end_date:
            qry = qry.filter(TaskInstance.execution_date <= end_date)

        tasks = [self.task_id]

        if upstream:
            tasks += [t.task_id for t in self.get_flat_relatives(upstream=True)]

        if downstream:
            tasks += [t.task_id for t in self.get_flat_relatives(upstream=False)]

        qry = qry.filter(TaskInstance.task_id.in_(tasks))
        results = qry.all()
        count = len(results)
        clear_task_instances(results, session, dag=self.dag)
        session.commit()
        return count

    @provide_session
    def get_task_instances(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        session: Session = None,
    ) -> List[TaskInstance]:
        """
        Get a set of task instance related to this task for a specific date
        range.
        """
        end_date = end_date or timezone.utcnow()
        return (
            session.query(TaskInstance)
            .filter(TaskInstance.dag_id == self.dag_id)
            .filter(TaskInstance.task_id == self.task_id)
            .filter(TaskInstance.execution_date >= start_date)
            .filter(TaskInstance.execution_date <= end_date)
            .order_by(TaskInstance.execution_date)
            .all()
        )

    def get_flat_relative_ids(
        self,
        upstream: bool = False,
        found_descendants: Optional[Set[str]] = None,
    ) -> Set[str]:
        """Get a flat set of relatives' ids, either upstream or downstream."""
        if not self._dag:
            return set()

        if not found_descendants:
            found_descendants = set()
        relative_ids = self.get_direct_relative_ids(upstream)

        for relative_id in relative_ids:
            if relative_id not in found_descendants:
                found_descendants.add(relative_id)
                relative_task = self._dag.task_dict[relative_id]
                relative_task.get_flat_relative_ids(upstream, found_descendants)

        return found_descendants

    def get_flat_relatives(self, upstream: bool = False):
        """Get a flat list of relatives, either upstream or downstream."""
        if not self._dag:
            return set()
        from airflow.models.dag import DAG

        dag: DAG = self._dag
        return list(map(lambda task_id: dag.task_dict[task_id], self.get_flat_relative_ids(upstream)))

    @provide_session
    def run(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        ignore_first_depends_on_past: bool = True,
        ignore_ti_state: bool = False,
        mark_success: bool = False,
        test_mode: bool = False,
        session: Session = None,
    ) -> None:
        """Run a set of task instances for a date range."""
        from airflow.models import DagRun
        from airflow.utils.types import DagRunType

        # Assertions for typing -- we need a dag, for this function, and when we have a DAG we are
        # _guaranteed_ to have start_date (else we couldn't have been added to a DAG)
        if TYPE_CHECKING:
            assert self.start_date

        start_date = pendulum.instance(start_date or self.start_date)
        end_date = pendulum.instance(end_date or self.end_date or timezone.utcnow())

        for info in self.dag.iter_dagrun_infos_between(start_date, end_date, align=False):
            ignore_depends_on_past = info.logical_date == start_date and ignore_first_depends_on_past
            try:
                dag_run = (
                    session.query(DagRun)
                    .filter(
                        DagRun.dag_id == self.dag_id,
                        DagRun.execution_date == info.logical_date,
                    )
                    .one()
                )
                ti = TaskInstance(self, run_id=dag_run.run_id)
            except NoResultFound:
                # This is _mostly_ only used in tests
                dr = DagRun(
                    dag_id=self.dag_id,
                    run_id=DagRun.generate_run_id(DagRunType.MANUAL, info.logical_date),
                    run_type=DagRunType.MANUAL,
                    execution_date=info.logical_date,
                    data_interval=info.data_interval,
                )
                ti = TaskInstance(self, run_id=dr.run_id)
                ti.dag_run = dr
                session.add(dr)
                session.flush()

            ti.run(
                mark_success=mark_success,
                ignore_depends_on_past=ignore_depends_on_past,
                ignore_ti_state=ignore_ti_state,
                test_mode=test_mode,
                session=session,
            )

    def dry_run(self) -> None:
        """Performs dry run for the operator - just render template fields."""
        self.log.info('Dry run')
        for field in self.template_fields:
            content = getattr(self, field)
            if content and isinstance(content, str):
                self.log.info('Rendering template for %s', field)
                self.log.info(content)

    def get_direct_relative_ids(self, upstream: bool = False) -> Set[str]:
        """
        Get set of the direct relative ids to the current task, upstream or
        downstream.
        """
        if upstream:
            return self._upstream_task_ids
        else:
            return self._downstream_task_ids

    def get_direct_relatives(self, upstream: bool = False) -> List["BaseOperator"]:
        """
        Get list of the direct relatives to the current task, upstream or
        downstream.
        """
        if upstream:
            return self.upstream_list
        else:
            return self.downstream_list

    def __repr__(self):
        return "<Task({self.task_type}): {self.task_id}>".format(self=self)

    @property
    def task_type(self) -> str:
        """@property: type of the task"""
        return self.__class__.__name__

    def add_only_new(self, item_set: Set[str], item: str, dag_id: str) -> None:
        """Adds only new items to item set"""
        if item in item_set:
            self.log.warning('Dependency %s, %s already registered for DAG: %s', self, item, dag_id)
        else:
            item_set.add(item)

    @property
    def roots(self) -> List["BaseOperator"]:
        """Required by TaskMixin"""
        return [self]

    @property
    def leaves(self) -> List["BaseOperator"]:
        """Required by TaskMixin"""
        return [self]

    def _set_relatives(
        self,
        task_or_task_list: Union[TaskMixin, Sequence[TaskMixin]],
        upstream: bool = False,
        edge_modifier: Optional[EdgeModifier] = None,
    ) -> None:
        """Sets relatives for the task or task list."""
        if not isinstance(task_or_task_list, Sequence):
            task_or_task_list = [task_or_task_list]

        task_list: List["BaseOperator"] = []
        for task_object in task_or_task_list:
            task_object.update_relative(self, not upstream)
            relatives = task_object.leaves if upstream else task_object.roots
            task_list.extend(relatives)

        for task in task_list:
            if not isinstance(task, BaseOperator):
                raise AirflowException(
                    f"Relationships can only be set between Operators; received {task.__class__.__name__}"
                )

        # relationships can only be set if the tasks share a single DAG. Tasks
        # without a DAG are assigned to that DAG.
        dags = {
            task._dag.dag_id: task._dag for task in self.roots + task_list if task.has_dag()  # type: ignore
        }

        if len(dags) > 1:
            raise AirflowException(
                f'Tried to set relationships between tasks in more than one DAG: {dags.values()}'
            )
        elif len(dags) == 1:
            dag = dags.popitem()[1]
        else:
            raise AirflowException(
                f"Tried to create relationships between tasks that don't have DAGs yet. "
                f"Set the DAG for at least one task and try again: {[self] + task_list}"
            )

        if dag and not self.has_dag():
            # If this task does not yet have a dag, add it to the same dag as the other task and
            # put it in the dag's root TaskGroup.
            self.dag = dag
            self.dag.task_group.add(self)

        for task in task_list:
            if dag and not task.has_dag():
                # If the other task does not yet have a dag, add it to the same dag as this task and
                # put it in the dag's root TaskGroup.
                task.dag = dag
                task.dag.task_group.add(task)
            if upstream:
                task.add_only_new(task.get_direct_relative_ids(upstream=False), self.task_id, self.dag.dag_id)
                self.add_only_new(self._upstream_task_ids, task.task_id, task.dag.dag_id)
                if edge_modifier:
                    edge_modifier.add_edge_info(self.dag, task.task_id, self.task_id)
            else:
                self.add_only_new(self._downstream_task_ids, task.task_id, task.dag.dag_id)
                task.add_only_new(task.get_direct_relative_ids(upstream=True), self.task_id, self.dag.dag_id)
                if edge_modifier:
                    edge_modifier.add_edge_info(self.dag, self.task_id, task.task_id)

    def set_downstream(
        self,
        task_or_task_list: Union[TaskMixin, Sequence[TaskMixin]],
        edge_modifier: Optional[EdgeModifier] = None,
    ) -> None:
        """
        Set a task or a task list to be directly downstream from the current
        task. Required by TaskMixin.
        """
        self._set_relatives(task_or_task_list, upstream=False, edge_modifier=edge_modifier)

    def set_upstream(
        self,
        task_or_task_list: Union[TaskMixin, Sequence[TaskMixin]],
        edge_modifier: Optional[EdgeModifier] = None,
    ) -> None:
        """
        Set a task or a task list to be directly upstream from the current
        task. Required by TaskMixin.
        """
        self._set_relatives(task_or_task_list, upstream=True, edge_modifier=edge_modifier)

    @property
    def output(self):
        """Returns reference to XCom pushed by current operator"""
        from airflow.models.xcom_arg import XComArg

        return XComArg(operator=self)

    @staticmethod
    def xcom_push(
        context: Any,
        key: str,
        value: Any,
        execution_date: Optional[datetime] = None,
    ) -> None:
        """
        Make an XCom available for tasks to pull.

        :param context: Execution Context Dictionary
        :type: Any
        :param key: A key for the XCom
        :type key: str
        :param value: A value for the XCom. The value is pickled and stored
            in the database.
        :type value: any pickleable object
        :param execution_date: if provided, the XCom will not be visible until
            this date. This can be used, for example, to send a message to a
            task on a future date without it being immediately visible.
        :type execution_date: datetime
        """
        context['ti'].xcom_push(key=key, value=value, execution_date=execution_date)

    @staticmethod
    def xcom_pull(
        context: Any,
        task_ids: Optional[List[str]] = None,
        dag_id: Optional[str] = None,
        key: str = XCOM_RETURN_KEY,
        include_prior_dates: Optional[bool] = None,
    ) -> Any:
        """
        Pull XComs that optionally meet certain criteria.

        The default value for `key` limits the search to XComs
        that were returned by other tasks (as opposed to those that were pushed
        manually). To remove this filter, pass key=None (or any desired value).

        If a single task_id string is provided, the result is the value of the
        most recent matching XCom from that task_id. If multiple task_ids are
        provided, a tuple of matching values is returned. None is returned
        whenever no matches are found.

        :param context: Execution Context Dictionary
        :type: Any
        :param key: A key for the XCom. If provided, only XComs with matching
            keys will be returned. The default key is 'return_value', also
            available as a constant XCOM_RETURN_KEY. This key is automatically
            given to XComs returned by tasks (as opposed to being pushed
            manually). To remove the filter, pass key=None.
        :type key: str
        :param task_ids: Only XComs from tasks with matching ids will be
            pulled. Can pass None to remove the filter.
        :type task_ids: str or iterable of strings (representing task_ids)
        :param dag_id: If provided, only pulls XComs from this DAG.
            If None (default), the DAG of the calling task is used.
        :type dag_id: str
        :param include_prior_dates: If False, only XComs from the current
            execution_date are returned. If True, XComs from previous dates
            are returned as well.
        :type include_prior_dates: bool
        """
        return context['ti'].xcom_pull(
            key=key, task_ids=task_ids, dag_id=dag_id, include_prior_dates=include_prior_dates
        )

    @cached_property
    def extra_links(self) -> List[str]:
        """@property: extra links for the task"""
        return list(
            set(self.operator_extra_link_dict.keys()).union(self.global_operator_extra_link_dict.keys())
        )

    def get_extra_links(self, dttm: datetime, link_name: str) -> Optional[Dict[str, Any]]:
        """
        For an operator, gets the URL that the external links specified in
        `extra_links` should point to.

        :raise ValueError: The error message of a ValueError will be passed on through to
            the fronted to show up as a tooltip on the disabled link
        :param dttm: The datetime parsed execution date for the URL being searched for
        :param link_name: The name of the link we're looking for the URL for. Should be
            one of the options specified in `extra_links`
        :return: A URL
        """
        if link_name in self.operator_extra_link_dict:
            return self.operator_extra_link_dict[link_name].get_link(self, dttm)
        elif link_name in self.global_operator_extra_link_dict:
            return self.global_operator_extra_link_dict[link_name].get_link(self, dttm)
        else:
            return None

    @classmethod
    def get_serialized_fields(cls):
        """Stringified DAGs and operators contain exactly these fields."""
        if not cls.__serialized_fields:
            from airflow.models.dag import DagContext

            # make sure the following dummy task is not added to current active
            # dag in context, otherwise, it will result in
            # `RuntimeError: dictionary changed size during iteration`
            # Exception in SerializedDAG.serialize_dag() call.
            DagContext.push_context_managed_dag(None)
            cls.__serialized_fields = frozenset(
                vars(BaseOperator(task_id='test')).keys()
                - {
                    'inlets',
                    'outlets',
                    '_upstream_task_ids',
                    'default_args',
                    'dag',
                    '_dag',
                    '_BaseOperator__instantiated',
                }
                | {
                    '_task_type',
                    'subdag',
                    'ui_color',
                    'ui_fgcolor',
                    'template_ext',
                    'template_fields',
                    'template_fields_renderers',
                    'params',
                }
            )
            DagContext.pop_context_managed_dag()

        return cls.__serialized_fields

    def is_smart_sensor_compatible(self):
        """Return if this operator can use smart service. Default False."""
        return False

    @property
    def inherits_from_dummy_operator(self):
        """Used to determine if an Operator is inherited from DummyOperator"""
        # This looks like `isinstance(self, DummyOperator) would work, but this also
        # needs to cope when `self` is a Serialized instance of a DummyOperator or one
        # of its sub-classes (which don't inherit from anything but BaseOperator).
        return getattr(self, '_is_dummy', False)

    def defer(
        self,
        *,
        trigger: BaseTrigger,
        method_name: str,
        kwargs: Optional[Dict[str, Any]] = None,
        timeout: Optional[timedelta] = None,
    ):
        """
        Marks this Operator as being "deferred" - that is, suspending its
        execution until the provided trigger fires an event.

        This is achieved by raising a special exception (TaskDeferred)
        which is caught in the main _execute_task wrapper.
        """
        raise TaskDeferred(trigger=trigger, method_name=method_name, kwargs=kwargs, timeout=timeout)


Chainable = Union[BaseOperator, "XComArg", EdgeModifier, "TaskGroup"]


def chain(*tasks: Union[Chainable, Sequence[Chainable]]) -> None:
    r"""
    Given a number of tasks, builds a dependency chain.

    This function accepts values of BaseOperator (aka tasks), EdgeModifiers (aka Labels), XComArg, TaskGroups,
    or lists containing any mix of these types (or a mix in the same list). If you want to chain between two
    lists you must ensure they have the same length.

    Using classic operators/sensors:

    .. code-block:: python

        chain(t1, [t2, t3], [t4, t5], t6)

    is equivalent to::

          / -> t2 -> t4 \
        t1               -> t6
          \ -> t3 -> t5 /

    .. code-block:: python

        t1.set_downstream(t2)
        t1.set_downstream(t3)
        t2.set_downstream(t4)
        t3.set_downstream(t5)
        t4.set_downstream(t6)
        t5.set_downstream(t6)

    Using task-decorated functions aka XComArgs:

    .. code-block:: python

        chain(x1(), [x2(), x3()], [x4(), x5()], x6())

    is equivalent to::

          / -> x2 -> x4 \
        x1               -> x6
          \ -> x3 -> x5 /

    .. code-block:: python

        x1 = x1()
        x2 = x2()
        x3 = x3()
        x4 = x4()
        x5 = x5()
        x6 = x6()
        x1.set_downstream(x2)
        x1.set_downstream(x3)
        x2.set_downstream(x4)
        x3.set_downstream(x5)
        x4.set_downstream(x6)
        x5.set_downstream(x6)

    Using TaskGroups:

    .. code-block:: python

        chain(t1, task_group1, task_group2, t2)

        t1.set_downstream(task_group1)
        task_group1.set_downstream(task_group2)
        task_group2.set_downstream(t2)


    It is also possible to mix between classic operator/sensor, EdgeModifiers, XComArg, and TaskGroups:

    .. code-block:: python

        chain(t1, [Label("branch one"), Label("branch two")], [x1(), x2()], task_group1, t2())

    is equivalent to::

          / "branch one" -> x1 \
        t1                      -> t2 -> x3
          \ "branch two" -> x2 /

    .. code-block:: python

        x1 = x1()
        x2 = x2()
        x3 = x3()
        label1 = Label("branch one")
        label2 = Label("branch two")
        t1.set_downstream(label1)
        label1.set_downstream(x1)
        t2.set_downstream(label2)
        label2.set_downstream(x2)
        x1.set_downstream(task_group1)
        x2.set_downstream(task_group1)
        task_group1.set_downstream(x3)

        # or

        x1 = x1()
        x2 = x2()
        x3 = x3()
        t1.set_downstream(x1, edge_modifier=Label("branch one"))
        t1.set_downstream(x2, edge_modifier=Label("branch two"))
        x1.set_downstream(task_group1)
        x2.set_downstream(task_group1)
        task_group1.set_downstream(x3)


    :param tasks: Individual and/or list of tasks, EdgeModifiers, XComArgs, or TaskGroups to set dependencies
    :type tasks: List[airflow.models.BaseOperator], airflow.models.BaseOperator,
        List[airflow.utils.EdgeModifier], airflow.utils.EdgeModifier, List[airflow.models.XComArg], XComArg,
        List[airflow.utils.TaskGroup], or airflow.utils.TaskGroup
    """
    from airflow.models.xcom_arg import XComArg
    from airflow.utils.task_group import TaskGroup

    chainable_types = (BaseOperator, XComArg, EdgeModifier, TaskGroup)

    for index, up_task in enumerate(tasks[:-1]):
        down_task = tasks[index + 1]
        if isinstance(up_task, chainable_types):
            up_task.set_downstream(down_task)
            continue
        if isinstance(down_task, chainable_types):
            down_task.set_upstream(up_task)
            continue
        if not isinstance(up_task, Sequence) or not isinstance(down_task, Sequence):
            raise TypeError(f'Chain not supported between instances of {type(up_task)} and {type(down_task)}')
        up_task_list = up_task
        down_task_list = down_task
        if len(up_task_list) != len(down_task_list):
            raise AirflowException(
                f'Chain not supported for different length Iterable. '
                f'Got {len(up_task_list)} and {len(down_task_list)}.'
            )
        for up_t, down_t in zip(up_task_list, down_task_list):
            up_t.set_downstream(down_t)


def cross_downstream(
    from_tasks: Sequence[Union[BaseOperator, "XComArg"]],
    to_tasks: Union[BaseOperator, "XComArg", Sequence[Union[BaseOperator, "XComArg"]]],
):
    r"""
    Set downstream dependencies for all tasks in from_tasks to all tasks in to_tasks.

    Using classic operators/sensors:

    .. code-block:: python

        cross_downstream(from_tasks=[t1, t2, t3], to_tasks=[t4, t5, t6])

    is equivalent to::

        t1 ---> t4
           \ /
        t2 -X -> t5
           / \
        t3 ---> t6

    .. code-block:: python

        t1.set_downstream(t4)
        t1.set_downstream(t5)
        t1.set_downstream(t6)
        t2.set_downstream(t4)
        t2.set_downstream(t5)
        t2.set_downstream(t6)
        t3.set_downstream(t4)
        t3.set_downstream(t5)
        t3.set_downstream(t6)

    Using task-decorated functions aka XComArgs:

    .. code-block:: python

        cross_downstream(from_tasks=[x1(), x2(), x3()], to_tasks=[x4(), x5(), x6()])

    is equivalent to::

        x1 ---> x4
           \ /
        x2 -X -> x5
           / \
        x3 ---> x6

    .. code-block:: python

        x1 = x1()
        x2 = x2()
        x3 = x3()
        x4 = x4()
        x5 = x5()
        x6 = x6()
        x1.set_downstream(x4)
        x1.set_downstream(x5)
        x1.set_downstream(x6)
        x2.set_downstream(x4)
        x2.set_downstream(x5)
        x2.set_downstream(x6)
        x3.set_downstream(x4)
        x3.set_downstream(x5)
        x3.set_downstream(x6)

    It is also possible to mix between classic operator/sensor and XComArg tasks:

    .. code-block:: python

        cross_downstream(from_tasks=[t1, x2(), t3], to_tasks=[x1(), t2, x3()])

    is equivalent to::

        t1 ---> x1
           \ /
        x2 -X -> t2
           / \
        t3 ---> x3

    .. code-block:: python

        x1 = x1()
        x2 = x2()
        x3 = x3()
        t1.set_downstream(x1)
        t1.set_downstream(t2)
        t1.set_downstream(x3)
        x2.set_downstream(x1)
        x2.set_downstream(t2)
        x2.set_downstream(x3)
        t3.set_downstream(x1)
        t3.set_downstream(t2)
        t3.set_downstream(x3)

    :param from_tasks: List of tasks or XComArgs to start from.
    :type from_tasks: List[airflow.models.BaseOperator] or List[airflow.models.XComArg]
    :param to_tasks: List of tasks or XComArgs to set as downstream dependencies.
    :type to_tasks: List[airflow.models.BaseOperator] or List[airflow.models.XComArg]
    """
    for task in from_tasks:
        task.set_downstream(to_tasks)


@attr.s(auto_attribs=True)
class BaseOperatorLink(metaclass=ABCMeta):
    """Abstract base class that defines how we get an operator link."""

    operators: ClassVar[List[Type[BaseOperator]]] = []
    """
    This property will be used by Airflow Plugins to find the Operators to which you want
    to assign this Operator Link

    :return: List of Operator classes used by task for which you want to create extra link
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Name of the link. This will be the button name on the task UI.

        :return: link name
        """

    @abstractmethod
    def get_link(self, operator: BaseOperator, dttm: datetime) -> str:
        """
        Link to external system.

        :param operator: airflow operator
        :param dttm: datetime
        :return: link to external system
        """
