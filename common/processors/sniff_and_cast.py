"""This processor parses and casts amounts and dates by sniffing data.

Assumptions
-----------

At this stage we assume that there is only one data resource.

Data processing
---------------

The processor goes through all fields in the schema and assigns them a caster.
If the field has a complete set of format parameters, a ready made caster is
used, else the processor sniffs the data to find the best match. If the
processor fails to sniff a caster, a `CasterNotFound` error is raised.

Dates require the `format` parameter. Numbers require the `groupChar` and
`decimalChar` parameters. Parameters can be set in the datapackage.

Datapackage mutation
--------------------

The processor toggles field types to match changes in the data.

"""

# TODO: relax the single resource constraint in `sniff_and_cast` processor
import petl

from copy import deepcopy
from logging import warning, info
from datapackage_pipelines.wrapper import ingest, spew
from jsontableschema.exceptions import InvalidCastError
from jsontableschema.types import DateType, NumberType

from common.utilities import process, format_to_json
from common.config import (
    SNIFFER_SAMPLE_SIZE,
    SNIFFER_MAX_FAILURE_RATIO,
    DATE_FORMATS,
    NUMBER_FORMATS
)

import logging


class CasterNotFound(Exception):
    """A chatty exception class for when the sniffer fails."""

    template = (
        'Could not find a parser for {field}\n'
        'Tried\n{guesses} on {sample_size} rows\n'
        'Failed values =\n{failed_rows}\n'
        'Failed {nb_failures} times (maximum allowed = {max_nb_failures})'
    )

    def __init__(self, sniffer):
        self.sniffer = sniffer
        super(CasterNotFound, self).__init__(self._message)

    @property
    def _message(self):
        return self.template.format(
            field=self.sniffer.field['name'],
            guesses=format_to_json(self.sniffer.format_guesses),
            failed_rows=format_to_json(self.sniffer.failures[:10]),
            nb_failures=self.sniffer.nb_failures,
            max_nb_failures=self.sniffer.max_nb_failures,
            sample_size=self.sniffer.sample_size,
        )


class BaseSniffer(object):
    """A class that tries very hard to find an appropriate caster."""

    jst_type_class = None
    format_keys = []
    format_guesses = []

    def __init__(self, field, resource_sample, max_failure_rate):
        self.max_failure_rate = max_failure_rate
        self.sample_values = self._get_field_sample(resource_sample, field)
        self._nb_empty_sample_values = self.sample_values.count('')
        self.sample_size = len(
            self.sample_values) - self._nb_empty_sample_values
        self.max_nb_failures = 0

        # The following get updated with each guess
        self.format = {key: field.get(key) for key in self.format_keys}
        self.field = deepcopy(field)
        self.nb_failures = 0
        self.failures = []
        self.casters = []
        self.init_casters(field)

    def init_casters(self, field):
        casters = []
        if all(self.format.values()):
            _field = deepcopy(field).update(self.format)
            casters.append((self.jst_type_class(_field), 0, self.format))

        for fmt in self.format_guesses:
            _field = deepcopy(field)
            _field.update(fmt)
            casters.append((self.jst_type_class(_field), 0, deepcopy(fmt)))

        for raw_value in self.sample_values:
            if raw_value:
                casted_value = None
                for idx in range(len(casters)):
                    caster, successes, fmt = casters[idx]
                    try:
                        assert self._pre_cast_checks_ok(fmt, raw_value)
                        casted_value = caster.cast(raw_value)
                        assert self._post_cast_check_ok(fmt, casted_value)
                        casters[idx] = (caster, successes+1, fmt)
                        break
                    except (AssertionError, InvalidCastError) as e:
                        pass
                if casted_value is None:
                    self.nb_failures += 1
                    self.failures.append(raw_value)

        casters.sort(key=lambda x: x[1], reverse=True)
        self.casters = casters

        if self.nb_failures <= self.max_nb_failures:
            self._log_success()
            return

        raise CasterNotFound(self)

    def cast(self, raw_value):
        exc = None
        for caster, _, _ in self.casters:
            try:
                return caster.cast(raw_value)
            except InvalidCastError as e:
                exc = e
        assert exc is not None
        raise exc

    def _pre_cast_checks_ok(self, fmt, value):
        return True

    def _post_cast_check_ok(self, fmt, value):
        return True

    @staticmethod
    def _get_field_sample(resource_sample, field):
        """Return a subset of the relevant data column."""

        sample_table = petl.fromdicts(resource_sample)
        sample_column = list(petl.values(sample_table, field['name']))
        return sample_column

    def _log_success(self):
        message = (
            'Caster guess for %s is %s, '
            'number of failures = %s (allowed %s), '
            'sample size = %s (%s empty values)'
        )
        args = (
            self.field['name'], self,
            self.nb_failures,
            self.max_nb_failures,
            self.sample_size,
            self._nb_empty_sample_values
        )
        info(message, *args)

    def __str__(self):
        return '{} {}'.format(self.__class__.__name__, [x[2] for x in self.casters[:3]])

    def __repr__(self):
        return '<{}>'.format(self)


class DateSniffer(BaseSniffer):
    jst_type_class = DateType
    format_keys = ['format']
    format_guesses = DATE_FORMATS


class NumberSniffer(BaseSniffer):
    jst_type_class = NumberType
    format_keys = ['decimalChar', 'groupChar']
    format_guesses = NUMBER_FORMATS

    def _pre_cast_checks_ok(self, fmt, value):
        if value is not None:
            if value.count(fmt['decimalChar']) > 1:
                return False

            if fmt['decimalChar'] in value:
                decimal_index = value.find(fmt['decimalChar'])
                group_index = value.find(fmt['groupChar'])
                if decimal_index < group_index:
                    return False

        return True

    # noinspection PyMethodMayBeStatic
    def _post_cast_check_ok(self, fmt, value):
        if value is not None:
            value_as_string = str(value)
            if '.' in value_as_string:
                if len(value_as_string.split('.')[1]) > 2:
                    return False

        return True


def select_sniffer(field):
    """Select the sniffer according to the fiscal field type."""

    assert field['type'] in ('date', 'number')
    sniffer_name = field['type'].capitalize() + 'Sniffer'
    sniffer_class = globals()[sniffer_name]
    return sniffer_class


def get_casters(datapackage,
                resource_sample,
                max_failure_rate=SNIFFER_MAX_FAILURE_RATIO):
    """Return a caster for each fiscal field."""

    casters = {}

    for field in datapackage['resources'][0]['schema']['fields']:
        if field['type'] == 'string':
            sniffer = None

        else:
            sniffer_class = select_sniffer(field)
            sniffer = sniffer_class(field, resource_sample, max_failure_rate)

        casters.update({field['name']: sniffer})

    return casters


def cast_values(row, casters, row_index=None):
    """Convert values to JSONTableSchema types."""

    for key, value in row.items():
        if key in casters:
            if casters[key] is not None:
                if value is not None and len(value.strip()) > 0:
                    try:
                        row[key] = casters[key].cast(value)
                    except InvalidCastError:
                        message = 'Could not cast %s = %s'
                        warning(message, key, row[key])
                        assert False, message

        else:
            if row_index == 0:
                message = (
                    '%s field is not in the datapackage, '
                    'data is out of sync with schema'
                )
                warning(message, key)

    return row


def extract_data_sample(resource, sample_size=SNIFFER_SAMPLE_SIZE):
    """Extract sample rows out of the resource."""

    data_sample = []

    for i, row in enumerate(resource):
        data_sample.append(row)
        if i + 1 == sample_size:
            break
    return data_sample, resource


def concatenate_data_sample(data_sample, resource):
    """Concatenate the sample rows back with the rest of the resource."""

    i = 0
    for row in data_sample:
        i+=1
        yield row
    for row in resource:
        i+=1
        yield row


if __name__ == '__main__':
    _, datapackage_, resources_ = ingest()
    resources_ = list(resources_)
    resource_ = resources_[0]
    assert(len(resources_)==1)
    resource_sample_, resource_left_over_ = extract_data_sample(resource_)
    casters_ = get_casters(datapackage_, resource_sample_)
    resource_ = concatenate_data_sample(resource_sample_, resource_left_over_)
    kwargs = dict(casters=casters_, pass_row_index=True)
    new_resources_ = process([resource_], cast_values, **kwargs)
    spew(datapackage_, new_resources_)
    import logging
