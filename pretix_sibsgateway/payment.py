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

# This file is based on an earlier version of pretix which was released under the Apache License 2.0. The full text of
# the Apache License 2.0 can be obtained at <http://www.apache.org/licenses/LICENSE-2.0>.
#
# This file may have since been changed and any changes are released under the terms of AGPLv3 as described above. A
# full history of changes and contributors is available at <https://github.com/pretix/pretix>.
#
# This file contains Apache-licensed contributions copyrighted by: Jakob Schnell, Tobias Kunze
#
# Unless required by applicable law or agreed to in writing, software distributed under the Apache License 2.0 is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under the License.

import json
import requests
import logging
import urllib.parse
from collections import OrderedDict
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core import signing
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext as __, gettext_lazy as _
from i18nfield.strings import LazyI18nString

from pretix.base.decimal import round_decimal
from pretix.base.models import Event, Order, OrderPayment, OrderRefund, Quota
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.services.mail import SendMailException
from pretix.base.settings import SettingsSandbox
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri
from pretix.plugins.paypal.models import ReferencedPayPalObject

logger = logging.getLogger('pretix.plugins.mbway_via_ifthenpay')

SUPPORTED_CURRENCIES = ['EUR']


class MBWAYViaIfThenPay(BasePaymentProvider):
    identifier = 'mbway_via_ifthenpay'
    verbose_name = _('MBWAY via IfThenPay')
    payment_form_fields = OrderedDict([
    ])

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox('payment', 'ifthenpay', event)
        self.ifthenpay_result = ''

    @property
    def test_mode_message(self):
        if self.settings.environment == 'test':
            return _('The IfThenPay is being used in test mode')
        return None

    @property
    def settings_form_fields(self):
        fields = [
            ('gateway_key',
             forms.CharField(
                 label=_('Gateway Key'),
                 required=True,
                 help_text=_('<a target="_blank" rel="noopener" href="{docs_url}">{text}</a>').format(
                     text=_('Click here for more information'),
                     docs_url='https://ifthenpay.com/'
                 )
             )),
            ('mb_way_key',
             forms.CharField(
                 label=_('MB WAY Key'),
                 required=True,
                 help_text=_('<a target="_blank" rel="noopener" href="{docs_url}">{text}</a>').format(
                     text=_('Click here for more information'),
                     docs_url='https://ifthenpay.com/'
                 )
             )),
            ('environment',
             forms.ChoiceField(
                 label=_('Environment'),
                 initial='live',
                 choices=(
                     ('live', 'Live'),
                     ('test', 'Test'),
                 ),
             )),
        ]

        extra_fields = [
            ('description',
             forms.CharField(
                 label=_('Reference description'),
                 help_text=_('Any value entered here will be added to the call'),
                 required=False,
             )),
        ]

        d = OrderedDict(
            fields + extra_fields + list(super().settings_form_fields.items())
        )

        d.move_to_end('description')
        d.move_to_end('_enabled', False)
        return d

    def is_allowed(self, request: HttpRequest, total: Decimal = None) -> bool:
        return super().is_allowed(request, total) and self.event.currency in SUPPORTED_CURRENCIES

    def payment_is_valid_session(self, request):
        return (request.session.get('payment_paypal_id', '') != ''
                and request.session.get('payment_paypal_payer', '') != '')

    def payment_form_render(self, request) -> str:
        template = get_template('templates/pretix_mbway/checkout_payment_form.html')
        ctx = {}
        return template.render(ctx)

    def checkout_prepare(self, request, cart):
        return super().checkout_prepare()

    def format_price(self, value: float):
        return f'{value: .2f}'

    @property
    def abort_pending_allowed(self):
        return False

    def checkout_confirm_render(self, request) -> str:
        """
        Returns the HTML that should be displayed when the user selected this provider
        on the 'confirm order' page.
        """
        template = get_template('templates/pretix_mbway/checkout_payment_confirm.html')
        # ctx = {'request': request, 'event': self.event, 'settings': self.settings}
        ctx = {}
        return template.render(ctx)

    def get_order_id(self, payment) -> str:
        return str(payment.local_id%(10**15))

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        key_gateway = self.settings.get('gateway_key', '')
        id_order = self.get_order_id(payment)
        amount = self.format_price(payment.amount)
        description = self.settings.get('description', '')
        language = request.headers.get('locale', 'en')
        key_mb_way = self.settings.get('mb_way_key', '')

        if key_gateway == '' or id_order == '' or amount == '' or key_mb_way == '':
            logger.exception('IfThenPay Invalid Credentials')
            raise PaymentException(_('IfThenPay Invalid credentials'))

        api_by_get = f'https://ifthenpay.com/api/gateway/paybylink/get?gatewaykey={ key_gateway }&id={ id_order }&amount={ amount }&description={ description }&lang={ language }&accounts=MBWAY|{ key_mb_way }'
        self.ifthenpay_result = requests.get(api_by_get)

        if self.ifthenpay_result.status_code == '200':
            payment.state = payment.PAYMENT_STATE_PENDING
            payment.save()
            return self.ifthenpay_result
        else:
            return False

    def payment_pending_render(self, request, payment) -> str:
        template = get_template('templates/pretix_mbway/pending.html')
        ctx = {}
        return template.render(ctx)

    # def api_payment_details(self, payment: OrderPayment):
    #     sale_id = None
    #     for trans in payment.info_data.get('transactions', []):
    #         for res in trans.get('related_resources', []):
    #             if 'sale' in res and 'id' in res['sale']:
    #                 sale_id = res['sale']['id']
    #     return {
    #         "payer_email": payment.info_data.get('payer', {}).get('payer_info', {}).get('email'),
    #         "payer_id": payment.info_data.get('payer', {}).get('payer_info', {}).get('payer_id'),
    #         "cart_id": payment.info_data.get('cart', None),
    #         "payment_id": payment.info_data.get('id', None),
    #         "sale_id": sale_id,
    #     }

    def payment_control_render(self, request: HttpRequest, payment: OrderPayment):
        template = get_template('templates/pretix_mbway/control.html')

        id_order = self.get_order_id(payment)
        amount = self.format_price(payment.amount)
        description = self.settings.get('description', '')
        language = request.headers.get('locale', 'en')
        status = payment.state

        ctx = {'id_order': id_order, 'amount': amount, 'description': description, 'language': language, 'status': status}
        return template.render(ctx)

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        return self.get_order_id(payment)

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return False

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return False

    def payment_prepare(self, request, payment_obj):
        # check quotas, if some quota is 0  raise exception
        # then check time and set the expiry date
        # reserve quota for a safe extra from after expiry time

        super().payment_prepare(request, payment_obj)
        pass
    
    def matching_id(self, payment: OrderPayment):
        return self.get_order_id(payment)

    def shred_payment_info(self, obj):
        super().shred_payment_info(obj)
        pass

    def render_invoice_text(self, order: Order, payment: OrderPayment) -> str:
        return super().render_invoice_text(order, payment)
