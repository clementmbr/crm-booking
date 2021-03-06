# Copyright 2020 Akretion
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class FeeDistributionWizard(models.TransientModel):
    _name = "fee.distribution.wizard"
    _rec_name = "lead_id"
    _description = "Distribute Fees and Commissions related to a lead"

    lead_id = fields.Many2one(
        string="Related lead", comodel_name="crm.lead", ondelete="set null",
    )
    distribution_line_ids = fields.One2many(
        string="Participant's Fee and Commission",
        comodel_name="fee.distribution.line.wizard",
        inverse_name="fee_distribution_wizard_id",
        help="Fees and Commissions distribution among the participants",
    )
    participant_ids = fields.Many2many(
        string="Participants",
        comodel_name="res.partner",
        help="Participants receiving the distributed Fees and Commission",
    )

    commission_calculation = fields.Selection(
        string="Commission Calculation",
        selection=[
            ("revenue", "% on revenue"),
            ("income", "% on income"),
            ("manual", "Manual"),
        ],
        default="manual",
        help="Calculation method for indicative commission total",
    )
    commission_percentage = fields.Float(
        string="Commission Percentage",
        default=10,
        help="% for indicative commission calculation",
    )

    company_id = fields.Many2one(related="lead_id.company_id", relation="res.company")
    company_currency = fields.Many2one(
        related="company_id.currency_id", relation="res.currency",
    )
    revenue = fields.Monetary(
        string="Revenue",
        currency_field="company_currency",
        related="lead_id.revenue_invoice_id.amount_total_company_signed",
        help="The Revenue invoice's amount",
    )
    income = fields.Monetary(
        string="Income",
        currency_field="company_currency",
        compute="_compute_income",
        help="Revenue less current Expenses (without including Taxes)",
    )
    commission_total = fields.Monetary(
        string="Total commission",
        currency_field="company_currency",
        compute="_compute_commission_total",
        help="Indicative total commission to be affected to the corresponding "
        "participants",
    )
    commission_to_distribute = fields.Monetary(
        string="Commission to distribute",
        currency_field="company_currency",
        compute="_compute_commission_to_distribute",
        help="Indicative leftover commission to be still affected to the corresponding "
        "participants",
    )
    max_fee = fields.Monetary(
        string="Maximum fee",
        currency_field="company_currency",
        compute="_compute_max_fee",
        help="Purely indicative value not to be exceeded in order to have a positive"
        "leftover.\nCalculated by (Revenue - Expenses - Commission) / Participants "
        "Number",
    )
    leftover = fields.Monetary(
        string="Leftover",
        currency_field="company_currency",
        compute="_compute_leftover",
        help="Indicative final leftover for the band.\n"
        "Result given by subtracting Expenses, Fees and Commissions to Revenue.\n"
        "⚠️ Does not include taxes expenses.",
    )

    @api.onchange("distribution_line_ids")
    def _onchange_distribution_line_ids(self):
        """Update  participant_ids when adding or removing one.
        Used to change dynamically participant_id's domain in distribution line"""
        for wiz in self:
            wiz.participant_ids = [
                (6, 0, wiz.distribution_line_ids.mapped("participant_id").ids)
            ]

    def _sum_lines_prod_category(self, inv, cat):
        """Returns the subtotal of invoice lines from a specific product category
        with no tax included"""
        line_ids = inv.invoice_line_ids.filtered(lambda l: l.product_id.categ_id == cat)
        return sum(line_ids.mapped("price_subtotal"))

    @api.depends("lead_id")
    def _compute_income(self):
        """Result from revenue invoice's amount (in company currency) less Expenses,
        without including taxes amount"""
        categ_id = self.env.ref("band_accounting.prod_categ_expense")
        for wiz in self:
            wiz.income = wiz.revenue
            for inv in self.lead_id.participant_invoice_ids:
                wiz.income -= self._sum_lines_prod_category(inv, categ_id)

    @api.depends("revenue", "income", "commission_calculation", "commission_percentage")
    def _compute_commission_total(self):
        for wiz in self:
            com_perc = wiz.commission_percentage
            if wiz.commission_calculation == "revenue":
                wiz.commission_total = (com_perc / 100) * wiz.revenue
            elif wiz.commission_calculation == "income":
                wiz.commission_total = (com_perc / 100) * wiz.income

    @api.depends("commission_total", "distribution_line_ids")
    def _compute_commission_to_distribute(self):
        for wiz in self:
            distributed_com = sum(wiz.distribution_line_ids.mapped("commission_amount"))
            wiz.commission_to_distribute = wiz.commission_total - distributed_com

    @api.depends("income", "commission_total", "distribution_line_ids")
    def _compute_max_fee(self):
        for wiz in self:
            amount_availabe = wiz.income - wiz.commission_total
            if wiz.participant_ids:
                wiz.max_fee = amount_availabe / len(wiz.participant_ids)
            else:
                wiz.max_fee = 0.00

    @api.depends("income", "commission_total", "distribution_line_ids")
    def _compute_leftover(self):
        for wiz in self:
            distributed_com = sum(wiz.distribution_line_ids.mapped("commission_amount"))
            distributed_fee = sum(wiz.distribution_line_ids.mapped("fee_amount"))
            wiz.leftover = wiz.income - distributed_com - distributed_fee

    def _fill_invoice(self, line, categ_type):
        """Create (if needed) or fill a `line`'s partner invoice with the fee or
        commission given in its line.
        `categ_type` must be the string 'fee' or 'commission'
        """
        assert categ_type in ["fee", "commission"]

        partner_id = line.participant_id
        # Existing partner's invoices in related lead
        partner_inv_ids = self.lead_id.participant_invoice_ids.filtered(
            lambda inv: inv.partner_id == partner_id
        )

        actual_amount = 0.00
        to_fill = 0.00
        if categ_type == "fee":
            product_id = line.fee_product_id
            categ_id = self.env.ref("band_accounting.prod_categ_fee")
            for inv in partner_inv_ids:
                actual_amount += self._sum_lines_prod_category(inv, categ_id)
            to_fill = line.fee_amount - actual_amount
        if categ_type == "commission":
            product_id = line.commission_product_id
            categ_id = self.env.ref("band_accounting.prod_categ_commission")
            for inv in partner_inv_ids:
                actual_amount += self._sum_lines_prod_category(inv, categ_id)
            to_fill = line.commission_amount - actual_amount

        if to_fill < 0:
            raise UserError(
                _(
                    "{} is already receiving a greater fee or commission than "
                    "the one filled here.\nPlease change its actual bills "
                    "before continuing.".format(partner_id.name)
                )
            )
        elif to_fill == 0:
            return True
        else:
            non_paid_inv_ids = partner_inv_ids.filtered(
                lambda inv: inv.state not in ["paid", "cancelled"]
            )
            if not partner_inv_ids or not non_paid_inv_ids:
                # Create a new invoice to be filled if there is no invoice or
                # only paid invoices
                vals = {
                    "type": "in_invoice",
                    "partner_id": partner_id.id,
                    "bill_lead_id": self.lead_id.id,
                }
                # Using play_onchanges method from onchange_helper OCA addon.
                # It adds to 'vals' the 'account_id' value filled by the
                # onchange methods triggered by filling an invoice's
                # 'partner_id' field from the UI.
                vals = self.env["account.invoice"].play_onchanges(vals, ["partner_id"])
                inv_to_fill_id = self.env["account.invoice"].create(vals)
            else:
                # If a non-paid invoice already exists, delete its fee/commission lines
                inv_to_fill_id = non_paid_inv_ids[0]
                fee_line_ids = inv_to_fill_id.invoice_line_ids.filtered(
                    lambda l: l.product_id.categ_id == categ_id
                )
                # Catch the deleted amount before deleting the lines
                to_fill += sum(fee_line_ids.mapped("price_subtotal"))
                fee_line_ids.unlink()

            # Add a new fee invoice line to inv_to_fill_id
            vals = {
                "invoice_id": inv_to_fill_id.id,
                "product_id": product_id.id,
                "quantity": 1,
                "price_unit": to_fill,
            }
            # Add 'uom_id', 'invoice_line_tax_ids' and other invoice line's
            # fields calculated by the onchange method when filling 'product_id'
            vals = self.env["account.invoice.line"].play_onchanges(
                vals, list(vals.keys())
            )
            self.env["account.invoice.line"].create(vals)
            inv_to_fill_id.compute_taxes()

    def action_fill_invoices(self):
        for wiz in self:
            for line in wiz.distribution_line_ids:
                self._fill_invoice(line, "fee")
                self._fill_invoice(line, "commission")

        return True


class FeeDistributionLineWizard(models.TransientModel):
    _name = "fee.distribution.line.wizard"
    _description = "Fee and Commission to a participant"

    def _default_commission_prod_id(self):
        """Returns the first product catched in the Commission category"""
        # TODO: Allows the user to define a default Commission product
        categ_commission = self.env.ref("band_accounting.prod_categ_commission")
        prod_commission_ids = self.env["product.product"].search(
            [("categ_id", "=", categ_commission.id)]
        )
        return prod_commission_ids[0]

    def _default_fee_prod_id(self):
        """Returns the first product catched in the Fee category"""
        # TODO: Allows the user to define a default Fee product
        categ_fee = self.env.ref("band_accounting.prod_categ_fee")
        prod_fee_ids = self.env["product.product"].search(
            [("categ_id", "=", categ_fee.id)]
        )
        return prod_fee_ids[0]

    fee_distribution_wizard_id = fields.Many2one(
        string="Fee Distribution Wizard",
        comodel_name="fee.distribution.wizard",
        ondelete="cascade",
    )

    participant_id = fields.Many2one(
        string="Participant",
        comodel_name="res.partner",
        ondelete="cascade",
        domain=[("supplier", "=", True)],
        required=True,
    )

    fee_product_id = fields.Many2one(
        string="Fee product",
        comodel_name="product.product",
        domain=lambda self: [
            ("categ_id", "=", self.env.ref("band_accounting.prod_categ_fee").id)
        ],
        default=_default_fee_prod_id,
        ondelete="cascade",
    )

    commission_product_id = fields.Many2one(
        string="Commission product",
        comodel_name="product.product",
        domain=lambda self: [
            ("categ_id", "=", self.env.ref("band_accounting.prod_categ_commission").id)
        ],
        default=_default_commission_prod_id,
        ondelete="cascade",
    )

    fee_amount = fields.Monetary(
        string="Fee Amount",
        currency_field="company_currency",
        help="The Fee value (without taxes) for the Fee product that will fill "
        "the participant's bill",
    )

    commission_amount = fields.Monetary(
        string="Commission Amount",
        currency_field="company_currency",
        help="The Commission value (without taxes) for the Commission product that "
        "will fill the participant's bill",
    )

    company_id = fields.Many2one(
        "res.company",
        string="Band",
        index=True,
        default=lambda self: self.fee_distribution_wizard_id.lead_id.company_id.id,
    )

    company_currency = fields.Many2one(
        string="Currency",
        related="company_id.currency_id",
        readonly=True,
        relation="res.currency",
    )

    # Intermediary field used to receive the participant's names from parent wizard's
    # participant_ids field and change participant_id's domain depending on it.
    participant_ids = fields.Many2many(
        string="Participants",
        comodel_name="res.partner",
        related="fee_distribution_wizard_id.participant_ids",
        help="Participants receiving the distributed Fees and Commission",
    )

    @api.onchange("fee_product_id")
    def _onchange_fee_product(self):
        self.ensure_one()
        self.fee_amount = self.fee_product_id.standard_price

    @api.onchange("commission_product_id")
    def _onchange_commission_product(self):
        self.ensure_one()
        self.commission_amount = self.commission_product_id.standard_price

    @api.onchange("participant_ids")
    def _onchange_participant_ids(self):
        """Change participant_id domain dynamically depending on parent wizard's
        participant_ids field"""
        self.ensure_one()
        participant_domain = [
            ("supplier", "=", True),
            ("id", "not in", self.participant_ids.ids),
        ]
        return {"domain": {"participant_id": participant_domain}}
