from __future__ import print_function, unicode_literals

import ply.lex as lex
import pytz
import operator
import re
import six

from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from django.db.models import Q
from functools import reduce
from ply import yacc
from temba.locations.models import AdminBoundary
from temba.utils import str_to_datetime
from temba.values.models import Value
from .models import ContactField, ContactURN


class SearchException(Exception):
    """
    Exception class for unparseable search queries
    """
    def __init__(self, message):
        self.message = message


class SearchLexer(object):
    """
    Lexer for complex search queries
    """
    t_LPAREN = r'\('
    t_RPAREN = r'\)'
    t_ignore = ' \t'  # ignore tabs and spaces

    tokens = ('AND', 'OR', 'COMPARATOR', 'TEXT', 'STRING', 'LPAREN', 'RPAREN')

    literals = '()'

    reserved = {
        'and': 'AND',
        'or': 'OR',
        'has': 'COMPARATOR',
        'is': 'COMPARATOR',
    }

    def __init__(self, **kwargs):
        self.lexer = lex.lex(module=self, reflags=re.UNICODE, **kwargs)

    def input(self, s):
        return self.lexer.input(s)

    def token(self):
        return self.lexer.token()

    def t_COMPARATOR(self, t):
        r"""(?i)~|=|[<>]=?|~~?"""
        return t

    def t_STRING(self, t):
        r"""("[^"]*")"""
        t.value = t.value[1:-1]
        return t

    def t_TEXT(self, t):
        r"""[\w_\.\+\-\/]+"""
        t.type = self.reserved.get(t.value.lower(), 'TEXT')
        return t

    def t_error(self, t):
        raise SearchException("Invalid character %s" % t.value[0])

    def test(self, data):  # pragma: no cover
        toks = []
        self.lexer.input(data)
        while True:
            tok = self.lexer.token()
            if not tok:
                break
            toks.append(tok)
        return toks


@six.python_2_unicode_compatible
class ContactQuery(object):
    """
    A parsed contact query consisting of a hierarchy of conditions and boolean combinations of conditions
    """
    PROP_ATTRIBUTE = 'A'
    PROP_SCHEME = 'S'
    PROP_FIELD = 'F'

    SEARCHABLE_ATTRIBUTES = ('name',)

    SEARCHABLE_SCHEMES = ('tel', 'twitter')

    def __init__(self, root):
        self.root = root

    def optimized(self):
        return ContactQuery(self.root.simplify().split_by_prop())

    def as_query(self, org):
        prop_map = self.get_prop_map(org)

        return self.root.as_query(org, prop_map)

    def get_prop_map(self, org):
        """
        Recursively collects all property names from this query and tries to match them to fields, searchable attributes
        and URN schemes.
        """
        prop_map = {p: None for p in self.root.get_prop_names() if p != Condition.IMPLICIT_PROP}

        for field in ContactField.objects.filter(org=org, key__in=prop_map.keys(), is_active=True):
            prop_map[field.key] = (self.PROP_FIELD, field)

        for attr in self.SEARCHABLE_ATTRIBUTES:
            if attr in prop_map.keys():
                prop_map[attr] = (self.PROP_ATTRIBUTE, attr)

        for scheme in self.SEARCHABLE_SCHEMES:
            if scheme in prop_map.keys():
                prop_map[scheme] = (self.PROP_SCHEME, scheme)

        for prop, prop_obj in prop_map.items():
            if not prop_obj:
                raise SearchException("Unrecognized field: %s" % prop)

        return prop_map

    def __eq__(self, other):
        return isinstance(other, ContactQuery) and self.root == other.root

    def __str__(self):
        return six.text_type(self.root)

    def __repr__(self):
        return 'ContactQuery{%s}' % six.text_type(self)


class QueryNode(object):
    """
    A search query node which is either a condition or a boolean combination of other conditions
    """
    def simplify(self):
        return self

    def split_by_prop(self):
        return self

    def as_query(self, org, prop_map):
        pass


@six.python_2_unicode_compatible
class Condition(QueryNode):
    IMPLICIT_PROP = '*'

    TEXT_LOOKUPS = {'=': 'iexact', '~': 'icontains'}

    DECIMAL_LOOKUPS = {
        '=': 'exact',
        'is': 'exact',
        '>': 'gt',
        '>=': 'gte',
        '<': 'lt',
        '<=': 'lte'
    }

    DATETIME_LOOKUPS = {
        '=': '<equal>',
        'is': '<equal>',
        '>': 'gt',
        '>=': 'gte',
        '<': 'lt',
        '<=': 'lte'
    }

    LOCATION_LOOKUPS = {'=': 'iexact', '~': 'icontains'}

    COMPARATOR_ALIASES = {'is': '=', 'has': '~'}

    def __init__(self, prop, comparator, value):
        self.prop = prop
        self.comparator = self.COMPARATOR_ALIASES[comparator] if comparator in self.COMPARATOR_ALIASES else comparator
        self.value = value

    def get_prop_names(self):
        return [self.prop]

    def as_query(self, org, prop_map):
        # a value without a prop implies query against name or URN, e.g. "bob"
        if self.prop == self.IMPLICIT_PROP:
            return self._build_implicit_prop_query(org)

        prop_type, prop_obj = prop_map[self.prop]

        if prop_type == ContactQuery.PROP_FIELD:
            # empty string equality means contacts without that field set
            if self.comparator.lower() in ('=', 'is') and self.value == "":
                return ~Q(id__in=Value.objects.filter(contact_field=prop_obj).values('contact_id'))
            else:
                return self._build_value_query(prop_obj)
        elif prop_type == ContactQuery.PROP_SCHEME:
            if org.is_anon:
                return Q(id=-1)
            else:
                return self._build_urn_query(prop_obj)
        else:
            return self._build_attr_query(prop_obj)

    def _build_implicit_prop_query(self, org):
        name_query = Q(name__icontains=self.value)

        if org.is_anon:
            try:
                urn_query = Q(id=int(self.value))  # try id match for anon orgs
            except ValueError:
                urn_query = Q(id=-1)
        else:
            urn_query = Q(id__in=ContactURN.objects.filter(path__icontains=self.value).values('contact_id'))

        return name_query | urn_query

    def _build_attr_query(self, attr):
        lookup = self.TEXT_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException("Unsupported comparator %s for contact attribute" % self.comparator)

        return Q(**{'%s__%s' % (attr, lookup): self.value})

    def _build_urn_query(self, scheme):
        lookup = self.TEXT_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException("Unsupported comparator %s for URN" % self.comparator)

        return Q(id__in=ContactURN.objects.filter(**{'scheme': scheme, 'path__%s' % lookup: self.value}).values('contact_id'))

    def _build_value_query(self, field):
        return Q(id__in=Value.objects.filter(**self.build_value_query_params(field)).values('contact_id'))

    def build_value_query_params(self, field):
        if field.value_type == Value.TYPE_TEXT:
            return self._build_text_field_params(field)
        elif field.value_type == Value.TYPE_DECIMAL:
            return self._build_decimal_field_params(field)
        elif field.value_type == Value.TYPE_DATETIME:
            return self._build_datetime_field_params(field)
        elif field.value_type in (Value.TYPE_STATE, Value.TYPE_DISTRICT, Value.TYPE_WARD):
            return self._build_location_field_params(field)
        else:  # pragma: no cover
            raise ValueError("Unrecognized contact field type '%s'" % field.value_type)

    def _build_text_field_params(self, field):
        lookup = self.TEXT_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException("Unsupported comparator %s for text field" % self.comparator)

        return {'contact_field': field, 'string_value__%s' % lookup: self.value}

    def _build_decimal_field_params(self, field):
        lookup = self.DECIMAL_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException("Unsupported comparator %s for decimal field" % self.comparator)

        try:
            value = Decimal(self.value)
        except Exception:
            raise SearchException("Can't convert '%s' to a decimal" % self.value)

        return {'contact_field': field, 'decimal_value__%s' % lookup: value}

    def _build_datetime_field_params(self, field):
        lookup = self.DATETIME_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException("Unsupported comparator %s for datetime field" % self.comparator)

        # parse as localized date and then convert to UTC
        local_date = str_to_datetime(self.value, field.org.timezone, field.org.get_dayfirst(), fill_time=False)
        if not local_date:
            raise SearchException("Unable to parse date: %s" % self.value)

        value = local_date.astimezone(pytz.utc)

        if lookup == '<equal>':
            # check if datetime is between date and date + 1d, i.e. anytime in that 24 hour period
            return {'contact_field': field,
                    'datetime_value__gte': value, 'datetime_value__lt': value + timedelta(days=1)}
        elif lookup == 'lte':
            # check if datetime is less then date + 1d, i.e. that day and all previous
            return {'contact_field': field, 'datetime_value__lt': value + timedelta(days=1)}
        elif lookup == 'gt':
            # check if datetime is greater than or equal to date + 1d, i.e. day after and subsequent
            return {'contact_field': field, 'datetime_value__gte': value + timedelta(days=1)}
        else:
            return {'contact_field': field, 'datetime_value__%s' % lookup: value}

    def _build_location_field_params(self, field):
        lookup = self.LOCATION_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException("Unsupported comparator %s for location field" % self.comparator)

        locations = AdminBoundary.objects.filter(**{'name__%s' % lookup: self.value}).values('id')

        return {'contact_field': field, 'location_value__in': locations}

    def __eq__(self, other):
        return isinstance(other, Condition) and self.prop == other.prop and self.comparator == other.comparator and self.value == other.value

    def __str__(self):
        return '%s%s%s' % (self.prop, self.comparator, self.value)


@six.python_2_unicode_compatible
class BoolCombination(QueryNode):
    """
    A combination of two or more conditions using an AND or OR logical operation
    """
    AND = operator.and_
    OR = operator.or_

    def __init__(self, op, *children):
        self.op = op
        self.children = list(children)

    def get_prop_names(self):
        names = []
        for child in self.children:
            names += child.get_prop_names()
        return names

    def simplify(self):
        """
        The expression `x OR y OR z` will be parsed as `OR(OR(x, y), z)` but because the logical operators AND/OR are
        associative we can simplify this as `OR(x, y, z)`.
        """
        self.children = [c.simplify() for c in self.children]  # simplify our children first

        simplified = []

        for child in self.children:
            if isinstance(child, Condition):
                simplified.append(child)
            elif child.op != self.op:
                return self  # can't optimize if children are combined with a different boolean op
            else:
                simplified += child.children

        return BoolCombination(self.op, *simplified)

    def split_by_prop(self):
        """
        The expression `OR(a=1, b=2, a=3)` can be re-arranged to `OR(OR(a=1, a=3), b=2)` so that `a=1 OR a=3` can be
        more efficiently checked using a single query on `a`.
        """
        self.children = [c.split_by_prop() for c in self.children]  # split our children first

        children_by_prop = OrderedDict()
        for child in self.children:
            prop = child.prop if isinstance(child, Condition) else None
            if prop not in children_by_prop:
                children_by_prop[prop] = []
            children_by_prop[prop].append(child)

        new_children = []
        for prop, children in children_by_prop.items():
            if len(children) > 1 and prop is not None:
                new_children.append(SinglePropCombination(prop, self.op, *children))
            else:
                new_children += children

        if len(new_children) == 1:
            return new_children[0]

        return BoolCombination(self.op, *new_children)

    def as_query(self, org, prop_map):
        return reduce(self.op, [child.as_query(org, prop_map) for child in self.children])

    def __eq__(self, other):
        return isinstance(other, BoolCombination) and self.op == other.op and self.children == other.children

    def __str__(self):
        op = 'OR' if self.op == self.OR else 'AND'
        return '%s(%s)' % (op, ', '.join([six.text_type(c) for c in self.children]))


@six.python_2_unicode_compatible
class SinglePropCombination(BoolCombination):
    """
    A special case combination where all conditions are on the same property and so may be optimized to query the value
    table only once.
    """
    def __init__(self, prop, op, *children):
        assert all([isinstance(c, Condition) and c.prop == prop for c in children])

        self.prop = prop

        super(SinglePropCombination, self).__init__(op, *children)

    def as_query(self, org, prop_map):
        prop_type, prop_obj = prop_map[self.prop]

        if prop_type == ContactQuery.PROP_FIELD:
            value_queries = []
            for child in self.children:
                params = child.build_value_query_params(prop_obj)
                del params['contact_field']
                value_queries.append(Q(**params))

            # TODO optimize `a = 1 OR a = 2` to `a IN (1, 2)`

            value_query = Q(contact_field=prop_obj) & reduce(self.op, value_queries)

            return Q(id__in=Value.objects.filter(value_query).values('contact_id'))

        return super(SinglePropCombination, self).as_query(org, prop_map)

    def __eq__(self, other):
        return isinstance(other, SinglePropCombination) and self.prop == other.prop and super(SinglePropCombination, self).__eq__(other)

    def __str__(self):
        op = 'OR' if self.op == self.OR else 'AND'
        return '%s[%s](%s)' % (op, self.prop, ', '.join(['%s %s' % (c.comparator, c.value) for c in self.children]))


# ================================== Parser definition ==================================

precedence = (
    ('left', 'OR'),
    ('left', 'AND'),
)


def p_expression_and(p):
    """expression : expression AND expression"""
    p[0] = BoolCombination(BoolCombination.AND, p[1], p[3])


def p_expression_or(p):
    """expression : expression OR expression"""
    p[0] = BoolCombination(BoolCombination.OR, p[1], p[3])


def p_expression_implicit_and(p):
    """expression : expression expression %prec AND"""
    p[0] = BoolCombination(BoolCombination.AND, p[1], p[2])


def p_expression_grouping(p):
    """expression : LPAREN expression RPAREN"""
    p[0] = p[2]


def p_condition(p):
    """expression : TEXT COMPARATOR literal"""
    p[0] = Condition(p[1].lower(), p[2].lower(), p[3])


def p_condition_implicit(p):
    """expression : TEXT"""
    p[0] = Condition(Condition.IMPLICIT_PROP, '=', p[1])


def p_literal(p):
    """literal : TEXT
               | STRING"""
    p[0] = p[1]


def p_error(p):
    raise SearchException(("Syntax error at '%s'" % p.value) if p else "Syntax error")


search_lexer = SearchLexer()
tokens = search_lexer.tokens
search_parser = yacc.yacc(write_tables=False)


def parse_query(text, optimize=True):
    """
    Parses a text query but doesn't perform it
    """
    query = ContactQuery(search_parser.parse(text, lexer=search_lexer))
    return query.optimized() if optimize else query


def contact_search(org, text, base_queryset):
    """
    Performs the given contact query on the given base queryset
    """
    parsed = parse_query(text)
    query = parsed.as_query(org)

    return base_queryset.filter(query)


def extract_fields(org, text):
    """
    Extracts contact fields from the given text query
    """
    parsed = parse_query(text)
    prop_map = parsed.get_prop_map(org)
    return [prop_obj for (prop_type, prop_obj) in prop_map.values() if prop_type == ContactQuery.PROP_FIELD]
