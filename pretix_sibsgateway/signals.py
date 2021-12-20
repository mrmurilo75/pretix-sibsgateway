#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020 Raphael Michel and contributors
# Copyright (C) 2020-2021 rami.io GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
import json
from collections import OrderedDict

from django import forms
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from pretix.base.signals import (
    logentry_display, register_global_settings, register_payment_providers,
)


@receiver(register_payment_providers, dispatch_uid="mbway_via_ifthenpay")
def register_payment_provider(sender, **kwargs):
    from .payment import MBWAYViaIfThenPay
    return MBWAYViaIfThenPay


@receiver(signal=logentry_display, dispatch_uid="mbway_via_ifthenpay_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if logentry.action_type != 'pretix.plugins.mbway_via_ifthenpay.event':
        return

    data = json.loads(logentry.data)
    event_type = data.get('event_type')
    text = None
    plains = {
        'PAYMENT.SALE.COMPLETED': _('Payment completed.'),
        'PAYMENT.SALE.DENIED': _('Payment denied.'),
        'PAYMENT.SALE.REFUNDED': _('Payment refunded.'),
        'PAYMENT.SALE.REVERSED': _('Payment reversed.'),
        'PAYMENT.SALE.PENDING': _('Payment pending.'),
    }

    if event_type in plains:
        text = plains[event_type]
    else:
        text = event_type

    if text:
        return _('IfThenPay reported an event: {}').format(text)


@receiver(register_global_settings, dispatch_uid='mbway_via_ifthenpay_global_settings')
def register_global_settings(sender, **kwargs):
    return OrderedDict([
        ('payment_mbway_via_ifthenpay_mbway_key', forms.CharField(
            label=_('IfThenPay: MB WAY Key'),
            required=False,
        )),
        ('payment_mbway_via_ifthenpay_environment', forms.ChoiceField(
            label=_('IfThenPay: Environment'),
            initial='live',
            choices=(
                ('live', 'Live'),
                ('test', 'Test'),
            ),
        )),
    ])
