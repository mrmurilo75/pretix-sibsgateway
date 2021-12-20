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

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        if (request.session.get('payment_paypal_id', '') == '' or request.session.get('payment_paypal_payer', '') == ''):
            raise PaymentException(_('We were unable to process your payment. See below for details on how to '
                                     'proceed.'))

        self.init_api()
        pp_payment = paypalrestsdk.Payment.find(request.session.get('payment_paypal_id'))
        ReferencedPayPalObject.objects.get_or_create(order=payment.order, payment=payment, reference=pp_payment.id)
        if str(pp_payment.transactions[0].amount.total) != str(payment.amount) or pp_payment.transactions[0].amount.currency \
                != self.event.currency:
            logger.error('Value mismatch: Payment %s vs paypal trans %s' % (payment.id, str(pp_payment)))
            raise PaymentException(_('We were unable to process your payment. See below for details on how to '
                                     'proceed.'))

        return self._execute_payment(pp_payment, request, payment)

    def _execute_payment(self, payment, request, payment_obj):
        if payment.state == 'created':
            payment.replace([
                {
                    "op": "replace",
                    "path": "/transactions/0/item_list",
                    "value": {
                        "items": [
                            {
                                "name": '{prefix}{orderstring}{postfix}'.format(
                                    prefix='{} '.format(self.settings.prefix) if self.settings.prefix else '',
                                    orderstring=__('Order {slug}-{code}').format(
                                        slug=self.event.slug.upper(),
                                        code=payment_obj.order.code
                                    ),
                                    postfix=' {}'.format(self.settings.postfix) if self.settings.postfix else ''
                                ),
                                "quantity": 1,
                                "price": self.format_price(payment_obj.amount),
                                "currency": payment_obj.order.event.currency
                            }
                        ]
                    }
                },
                {
                    "op": "replace",
                    "path": "/transactions/0/description",
                    "value": '{prefix}{orderstring}{postfix}'.format(
                        prefix='{} '.format(self.settings.prefix) if self.settings.prefix else '',
                        orderstring=__('Order {order} for {event}').format(
                            event=request.event.name,
                            order=payment_obj.order.code
                        ),
                        postfix=' {}'.format(self.settings.postfix) if self.settings.postfix else ''
                    ),
                }
            ])
            try:
                payment.execute({"payer_id": request.session.get('payment_paypal_payer')})
            except paypalrestsdk.exceptions.ConnectionError as e:
                messages.error(request, _('We had trouble communicating with PayPal'))
                logger.exception('Error on creating payment: ' + str(e))

        for trans in payment.transactions:
            for rr in trans.related_resources:
                if hasattr(rr, 'sale') and rr.sale:
                    if rr.sale.state == 'pending':
                        messages.warning(request, _('PayPal has not yet approved the payment. We will inform you as '
                                                    'soon as the payment completed.'))
                        payment_obj.info = json.dumps(payment.to_dict())
                        payment_obj.state = OrderPayment.PAYMENT_STATE_PENDING
                        payment_obj.save()
                        return

        payment_obj.refresh_from_db()
        if payment.state == 'pending':
            messages.warning(request, _('PayPal has not yet approved the payment. We will inform you as soon as the '
                                        'payment completed.'))
            payment_obj.info = json.dumps(payment.to_dict())
            payment_obj.state = OrderPayment.PAYMENT_STATE_PENDING
            payment_obj.save()
            return

        if payment.state != 'approved':
            payment_obj.fail(info=payment.to_dict())
            logger.error('Invalid state: %s' % str(payment))
            raise PaymentException(_('We were unable to process your payment. See below for details on how to '
                                     'proceed.'))

        if payment_obj.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            logger.warning('PayPal success event even though order is already marked as paid')
            return

        try:
            payment_obj.info = json.dumps(payment.to_dict())
            payment_obj.save(update_fields=['info'])
            payment_obj.confirm()
        except Quota.QuotaExceededException as e:
            raise PaymentException(str(e))

        except SendMailException:
            messages.warning(request, _('There was an error sending the confirmation mail.'))
        return None

    def payment_pending_render(self, request, payment) -> str:
        template = get_template('templates/pretix_mbway/pending.html')
        ctx = {}
        return template.render(ctx)

    def matching_id(self, payment: OrderPayment):
        sale_id = None
        for trans in payment.info_data.get('transactions', []):
            for res in trans.get('related_resources', []):
                if 'sale' in res and 'id' in res['sale']:
                    sale_id = res['sale']['id']
        return sale_id or payment.info_data.get('id', None)

    def api_payment_details(self, payment: OrderPayment):
        sale_id = None
        for trans in payment.info_data.get('transactions', []):
            for res in trans.get('related_resources', []):
                if 'sale' in res and 'id' in res['sale']:
                    sale_id = res['sale']['id']
        return {
            "payer_email": payment.info_data.get('payer', {}).get('payer_info', {}).get('email'),
            "payer_id": payment.info_data.get('payer', {}).get('payer_info', {}).get('payer_id'),
            "cart_id": payment.info_data.get('cart', None),
            "payment_id": payment.info_data.get('id', None),
            "sale_id": sale_id,
        }

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

        try:
            sale = None
            for res in refund.payment.info_data['transactions'][0]['related_resources']:
                for k, v in res.items():
                    if k == 'sale':
                        sale = paypalrestsdk.Sale.find(v['id'])
                        break

            if not sale:
                pp_payment = paypalrestsdk.Payment.find(refund.payment.info_data['id'])
                for res in pp_payment.transactions[0].related_resources:
                    for k, v in res.to_dict().items():
                        if k == 'sale':
                            sale = paypalrestsdk.Sale.find(v['id'])
                            break

            pp_refund = sale.refund({
                "amount": {
                    "total": self.format_price(refund.amount),
                    "currency": refund.order.event.currency
                }
            })
        except paypalrestsdk.exceptions.ConnectionError as e:
            refund.order.log_action('pretix.event.order.refund.failed', {
                'local_id': refund.local_id,
                'provider': refund.provider,
                'error': str(e)
            })
            raise PaymentException(_('Refunding the amount via PayPal failed: {}').format(str(e)))
        if not pp_refund.success():
            refund.order.log_action('pretix.event.order.refund.failed', {
                'local_id': refund.local_id,
                'provider': refund.provider,
                'error': str(pp_refund.error)
            })
            raise PaymentException(_('Refunding the amount via PayPal failed: {}').format(pp_refund.error))
        else:
            sale = paypalrestsdk.Payment.find(refund.payment.info_data['id'])
            refund.payment.info = json.dumps(sale.to_dict())
            refund.info = json.dumps(pp_refund.to_dict())
            refund.done()

    def payment_prepare(self, request, payment_obj):
        self.init_api()

        try:
            if request.event.settings.payment_paypal_connect_user_id:
                try:
                    tokeninfo = Tokeninfo.create_with_refresh_token(request.event.settings.payment_paypal_connect_refresh_token)
                except BadRequest as ex:
                    ex = json.loads(ex.content)
                    messages.error(request, '{}: {} ({})'.format(
                        _('We had trouble communicating with PayPal'),
                        ex['error_description'],
                        ex['correlation_id'])
                    )
                    return

                # Even if the token has been refreshed, calling userinfo() can fail. In this case we just don't
                # get the userinfo again and use the payment_paypal_connect_user_id that we already have on file
                try:
                    userinfo = tokeninfo.userinfo()
                    request.event.settings.payment_paypal_connect_user_id = userinfo.email
                except UnauthorizedAccess:
                    pass

                payee = {
                    "email": request.event.settings.payment_paypal_connect_user_id,
                    # If PayPal ever offers a good way to get the MerchantID via the Identifity API,
                    # we should use it instead of the merchant's eMail-address
                    # "merchant_id": request.event.settings.payment_paypal_connect_user_id,
                }
            else:
                payee = {}

            payment = paypalrestsdk.Payment({
                'header': {'PayPal-Partner-Attribution-Id': 'ramiioSoftwareentwicklung_SP'},
                'intent': 'sale',
                'payer': {
                    "payment_method": "paypal",
                },
                "redirect_urls": {
                    "return_url": build_absolute_uri(request.event, 'plugins:paypal:return'),
                    "cancel_url": build_absolute_uri(request.event, 'plugins:paypal:abort'),
                },
                "transactions": [
                    {
                        "item_list": {
                            "items": [
                                {
                                    "name": '{prefix}{orderstring}{postfix}'.format(
                                        prefix='{} '.format(self.settings.prefix) if self.settings.prefix else '',
                                        orderstring=__('Order {slug}-{code}').format(
                                            slug=self.event.slug.upper(),
                                            code=payment_obj.order.code
                                        ),
                                        postfix=' {}'.format(self.settings.postfix) if self.settings.postfix else ''
                                    ),
                                    "quantity": 1,
                                    "price": self.format_price(payment_obj.amount),
                                    "currency": payment_obj.order.event.currency
                                }
                            ]
                        },
                        "amount": {
                            "currency": request.event.currency,
                            "total": self.format_price(payment_obj.amount)
                        },
                        "description": '{prefix}{orderstring}{postfix}'.format(
                            prefix='{} '.format(self.settings.prefix) if self.settings.prefix else '',
                            orderstring=__('Order {order} for {event}').format(
                                event=request.event.name,
                                order=payment_obj.order.code
                            ),
                            postfix=' {}'.format(self.settings.postfix) if self.settings.postfix else ''
                        ),
                        "payee": payee,
                        "custom": '{prefix}{slug}-{code}{postfix}'.format(
                            prefix='{} '.format(self.settings.prefix) if self.settings.prefix else '',
                            slug=self.event.slug.upper(),
                            code=payment_obj.order.code,
                            postfix=' {}'.format(self.settings.postfix) if self.settings.postfix else ''
                        ),
                    }
                ]
            })
            request.session['payment_paypal_payment'] = payment_obj.pk
            return self._create_payment(request, payment)
        except paypalrestsdk.exceptions.ConnectionError as e:
            messages.error(request, _('We had trouble communicating with PayPal'))
            logger.exception('Error on creating payment: ' + str(e))

    def shred_payment_info(self, obj):
        if obj.info:
            d = json.loads(obj.info)
            new = {
                'id': d.get('id'),
                'payer': {
                    'payer_info': {
                        'email': '█'
                    }
                },
                'update_time': d.get('update_time'),
                'transactions': [
                    {
                        'amount': t.get('amount')
                    } for t in d.get('transactions', [])
                ],
                '_shredded': True
            }
            obj.info = json.dumps(new)
            obj.save(update_fields=['info'])

        for le in obj.order.all_logentries().filter(action_type="pretix.plugins.paypal.event").exclude(data=""):
            d = le.parsed_data
            if 'resource' in d:
                d['resource'] = {
                    'id': d['resource'].get('id'),
                    'sale_id': d['resource'].get('sale_id'),
                    'parent_payment': d['resource'].get('parent_payment'),
                }
            le.data = json.dumps(d)
            le.shredded = True
            le.save(update_fields=['data', 'shredded'])

    def render_invoice_text(self, order: Order, payment: OrderPayment) -> str:
        if order.status == Order.STATUS_PAID:
            if payment.info_data.get('id', None):
                try:
                    return '{}\r\n{}: {}\r\n{}: {}'.format(
                        _('The payment for this invoice has already been received.'),
                        _('PayPal payment ID'),
                        payment.info_data['id'],
                        _('PayPal sale ID'),
                        payment.info_data['transactions'][0]['related_resources'][0]['sale']['id']
                    )
                except (KeyError, IndexError):
                    return '{}\r\n{}: {}'.format(
                        _('The payment for this invoice has already been received.'),
                        _('PayPal payment ID'),
                        payment.info_data['id']
                    )
            else:
                return super().render_invoice_text(order, payment)

        return self.settings.get('_invoice_text', as_type=LazyI18nString, default='')
