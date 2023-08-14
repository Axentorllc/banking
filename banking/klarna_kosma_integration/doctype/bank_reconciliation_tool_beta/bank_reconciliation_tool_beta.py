# Copyright (c) 2023, ALYF GmbH and contributors
# For license information, please see license.txt
import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.custom import ConstantColumn
from frappe.utils import cint, flt
from pypika.terms import Parameter, PseudoColumn

from erpnext import get_default_cost_center
from erpnext.accounts.doctype.bank_transaction.bank_transaction import (
	get_total_allocated_amount,
)
from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
	reconcile_vouchers,
	subtract_allocations,
)


class BankReconciliationToolBeta(Document):
	pass


@frappe.whitelist()
def get_bank_transactions(
	bank_account, from_date=None, to_date=None, order_by="date asc"
):
	# returns bank transactions for a bank account
	filters = []
	filters.append(["bank_account", "=", bank_account])
	filters.append(["docstatus", "=", 1])
	filters.append(["unallocated_amount", ">", 0.0])
	if to_date:
		filters.append(["date", "<=", to_date])
	if from_date:
		filters.append(["date", ">=", from_date])
	transactions = frappe.get_all(
		"Bank Transaction",
		fields=[
			"date",
			"deposit",
			"withdrawal",
			"currency",
			"description",
			"name",
			"bank_account",
			"company",
			"unallocated_amount",
			"reference_number",
			"party_type",
			"party",
			"bank_party_name",
			"bank_party_account_number",
			"bank_party_iban",
		],
		filters=filters,
		order_by=order_by,
	)
	return transactions


@frappe.whitelist()
def create_journal_entry_bts(
	bank_transaction_name,
	reference_number=None,
	reference_date=None,
	posting_date=None,
	entry_type=None,
	second_account=None,
	mode_of_payment=None,
	party_type=None,
	party=None,
	allow_edit=None,
):
	# Create a new journal entry based on the bank transaction
	bank_transaction = frappe.db.get_values(
		"Bank Transaction",
		bank_transaction_name,
		fieldname=["name", "deposit", "withdrawal", "bank_account"],
		as_dict=True,
	)[0]
	company_account = frappe.get_value(
		"Bank Account", bank_transaction.bank_account, "account"
	)
	account_type = frappe.db.get_value("Account", second_account, "account_type")
	if account_type in ["Receivable", "Payable"]:
		if not (party_type and party):
			frappe.throw(
				_("Party Type and Party is required for Receivable / Payable account {0}").format(
					second_account
				)
			)

	company = frappe.get_value("Account", company_account, "company")

	accounts = []
	# Multi Currency?
	accounts.append(
		{
			"account": second_account,
			"credit_in_account_currency": bank_transaction.deposit,
			"debit_in_account_currency": bank_transaction.withdrawal,
			"party_type": party_type,
			"party": party,
			"cost_center": get_default_cost_center(company),
		}
	)

	accounts.append(
		{
			"account": company_account,
			"bank_account": bank_transaction.bank_account,
			"credit_in_account_currency": bank_transaction.withdrawal,
			"debit_in_account_currency": bank_transaction.deposit,
			"cost_center": get_default_cost_center(company),
		}
	)

	journal_entry_dict = {
		"voucher_type": entry_type,
		"company": company,
		"posting_date": posting_date,
		"cheque_date": reference_date,
		"cheque_no": reference_number,
		"mode_of_payment": mode_of_payment,
	}
	journal_entry = frappe.new_doc("Journal Entry")
	journal_entry.update(journal_entry_dict)
	journal_entry.set("accounts", accounts)
	journal_entry.insert()

	if allow_edit:
		return journal_entry  # Return saved document

	journal_entry.submit()

	if bank_transaction.deposit > 0.0:
		paid_amount = bank_transaction.deposit
	else:
		paid_amount = bank_transaction.withdrawal

	vouchers = json.dumps(
		[
			{
				"payment_doctype": "Journal Entry",
				"payment_name": journal_entry.name,
				"amount": paid_amount,
			}
		]
	)

	return reconcile_vouchers(bank_transaction_name, vouchers)


@frappe.whitelist()
def create_payment_entry_bts(
	bank_transaction_name,
	reference_number=None,
	reference_date=None,
	party_type=None,
	party=None,
	posting_date=None,
	mode_of_payment=None,
	project=None,
	cost_center=None,
	allow_edit=None,
):
	# Create a new payment entry based on the bank transaction
	bank_transaction = frappe.db.get_values(
		"Bank Transaction",
		bank_transaction_name,
		fieldname=["name", "unallocated_amount", "deposit", "bank_account"],
		as_dict=True,
	)[0]
	paid_amount = bank_transaction.unallocated_amount
	payment_type = "Receive" if bank_transaction.deposit > 0.0 else "Pay"

	company_account = frappe.get_value(
		"Bank Account", bank_transaction.bank_account, "account"
	)
	company = frappe.get_value("Account", company_account, "company")
	payment_entry_dict = {
		"company": company,
		"payment_type": payment_type,
		"reference_no": reference_number,
		"reference_date": reference_date,
		"party_type": party_type,
		"party": party,
		"posting_date": posting_date,
		"paid_amount": paid_amount,
		"received_amount": paid_amount,
	}
	payment_entry = frappe.new_doc("Payment Entry")

	payment_entry.update(payment_entry_dict)

	if mode_of_payment:
		payment_entry.mode_of_payment = mode_of_payment
	if project:
		payment_entry.project = project
	if cost_center:
		payment_entry.cost_center = cost_center
	if payment_type == "Receive":
		payment_entry.paid_to = company_account
	else:
		payment_entry.paid_from = company_account

	payment_entry.validate()
	payment_entry.insert()

	if allow_edit:
		return payment_entry  # Return saved document

	payment_entry.submit()
	vouchers = json.dumps(
		[
			{
				"payment_doctype": "Payment Entry",
				"payment_name": payment_entry.name,
				"amount": paid_amount,
			}
		]
	)
	return reconcile_vouchers(bank_transaction_name, vouchers)


@frappe.whitelist()
def upload_bank_statement(**args):
	args = frappe._dict(args)
	bsi = frappe.new_doc("Bank Statement Import")

	if args.company:
		bsi.update(
			{
				"company": args.company,
			}
		)

	if args.bank_account:
		bsi.update({"bank_account": args.bank_account})

	bsi.save()
	return bsi  # Return saved document


@frappe.whitelist()
def auto_reconcile_vouchers(
	bank_account,
	from_date=None,
	to_date=None,
	filter_by_reference_date=None,
	from_reference_date=None,
	to_reference_date=None,
):
	# Auto reconcile vouchers with matching reference numbers
	frappe.flags.auto_reconcile_vouchers = True
	reconciled, partially_reconciled = set(), set()

	bank_transactions = get_bank_transactions(bank_account, from_date, to_date)
	for transaction in bank_transactions:
		linked_payments = get_linked_payments(
			transaction.name,
			["payment_entry", "journal_entry"],
			from_date,
			to_date,
			filter_by_reference_date,
			from_reference_date,
			to_reference_date,
		)

		if not linked_payments:
			continue

		vouchers = list(
			map(
				lambda entry: {
					"payment_doctype": entry[1],
					"payment_name": entry[2],
					"amount": entry[4],
				},
				linked_payments,
			)
		)

		unallocated_before = transaction.unallocated_amount
		transaction = reconcile_vouchers(transaction.name, json.dumps(vouchers))

		if transaction.status == "Reconciled":
			reconciled.add(transaction.name)
		elif flt(unallocated_before) != flt(transaction.unallocated_amount):
			partially_reconciled.add(transaction.name)  # Partially reconciled

	alert_message, indicator = "", "blue"
	if not partially_reconciled and not reconciled:
		alert_message = _("No matches occurred via auto reconciliation")

	if reconciled:
		alert_message += _("{0} {1} Reconciled").format(
			len(reconciled), _("Transactions") if len(reconciled) > 1 else _("Transaction")
		)
		alert_message += "<br>"
		indicator = "green"

	if partially_reconciled:
		alert_message += _("{0} {1} Partially Reconciled").format(
			len(partially_reconciled),
			_("Transactions") if len(partially_reconciled) > 1 else _("Transaction"),
		)
		indicator = "green"

	frappe.msgprint(title=_("Auto Reconciliation"), msg=alert_message, indicator=indicator)

	frappe.flags.auto_reconcile_vouchers = False
	return reconciled, partially_reconciled


@frappe.whitelist()
def get_linked_payments(
	bank_transaction_name,
	document_types=None,
	from_date=None,
	to_date=None,
	filter_by_reference_date=None,
	from_reference_date=None,
	to_reference_date=None,
):
	# get all matching payments for a bank transaction
	transaction = frappe.get_doc("Bank Transaction", bank_transaction_name)
	gl_account, company = frappe.db.get_value(
		"Bank Account", transaction.bank_account, ["account", "company"]
	)
	matching = check_matching(
		gl_account,
		company,
		transaction,
		document_types,
		from_date,
		to_date,
		filter_by_reference_date,
		from_reference_date,
		to_reference_date,
	)
	return subtract_allocations(gl_account, matching)


def subtract_allocations(gl_account, vouchers):
	"Look up & subtract any existing Bank Transaction allocations"
	copied = []
	for voucher in vouchers:
		rows = get_total_allocated_amount(voucher.get("doctype"), voucher.get("name"))
		amount = None
		for row in rows:
			if row["gl_account"] == gl_account:
				amount = row["total"]
				break

		if amount:
			voucher["paid_amount"] -= amount

		copied.append(voucher)
	return copied


def check_matching(
	bank_account,
	company,
	transaction,
	document_types,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	# combine all types of vouchers
	subquery = get_queries(
		bank_account,
		company,
		transaction,
		document_types,
		from_date,
		to_date,
		filter_by_reference_date,
		from_reference_date,
		to_reference_date,
	)
	filters = {
		"amount": transaction.unallocated_amount,
		"payment_type": "Receive" if transaction.deposit > 0.0 else "Pay",
		"reference_no": transaction.reference_number,
		"party_type": transaction.party_type,
		"party": transaction.party,
		"bank_account": bank_account,
	}

	matching_vouchers = []
	matching_vouchers.extend(
		get_loan_vouchers(bank_account, transaction, document_types, filters)
	)

	for query in subquery:
		matching_vouchers.extend(
			frappe.db.sql(
				query,
				filters,
				as_dict=1,
			)
		)
	return (
		sorted(matching_vouchers, key=lambda x: x["rank"], reverse=True)
		if matching_vouchers
		else []
	)


def get_queries(
	bank_account,
	company,
	transaction,
	document_types,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	# get queries to get matching vouchers
	account_from_to = "paid_to" if transaction.deposit > 0.0 else "paid_from"
	exact_match = "exact_match" in document_types
	queries = []

	# get matching queries from all the apps (except erpnext, to override)
	for method_name in frappe.get_hooks("get_matching_queries")[1:]:
		queries.extend(
			frappe.get_attr(method_name)(
				bank_account,
				company,
				transaction,
				document_types,
				exact_match,
				account_from_to,
				from_date,
				to_date,
				filter_by_reference_date,
				from_reference_date,
				to_reference_date,
			)
			or []
		)

	return queries


def get_matching_queries(
	bank_account,
	company,
	transaction,
	document_types,
	exact_match,
	account_from_to,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	queries = []
	exact_party_match = "exact_party_match" in document_types

	if "payment_entry" in document_types:
		query = get_pe_matching_query(
			exact_match,
			account_from_to,
			transaction,
			from_date,
			to_date,
			filter_by_reference_date,
			from_reference_date,
			to_reference_date,
			exact_party_match,
		)
		queries.append(query)

	if "journal_entry" in document_types:
		query = get_je_matching_query(
			exact_match,
			transaction,
			from_date,
			to_date,
			filter_by_reference_date,
			from_reference_date,
			to_reference_date,
		)
		queries.append(query)

	if transaction.deposit > 0.0 and "sales_invoice" in document_types:
		if "unpaid_invoices" in document_types:
			query = get_unpaid_si_matching_query(exact_match, exact_party_match)
			queries.append(query)
		else:
			query = get_si_matching_query(exact_match, exact_party_match)
			queries.append(query)

	if transaction.withdrawal > 0.0 and "purchase_invoice" in document_types:
		if "unpaid_invoices" in document_types:
			query = get_unpaid_pi_matching_query(exact_match, exact_party_match)
			queries.append(query)
		else:
			query = get_pi_matching_query(exact_match, exact_party_match)
			queries.append(query)

	if "bank_transaction" in document_types:
		query = get_bt_matching_query(exact_match, transaction, exact_party_match)
		queries.append(query)

	return queries


def get_loan_vouchers(bank_account, transaction, document_types, filters):
	vouchers = []
	exact_match = "exact_match" in document_types

	if transaction.withdrawal > 0.0 and "loan_disbursement" in document_types:
		vouchers.extend(get_ld_matching_query(bank_account, exact_match, filters))

	if transaction.deposit > 0.0 and "loan_repayment" in document_types:
		vouchers.extend(get_lr_matching_query(bank_account, exact_match, filters))

	return vouchers


def get_bt_matching_query(exact_match, transaction, exact_party_match):
	# get matching bank transaction query
	# find bank transactions in the same bank account with opposite sign
	# same bank account must have same company and currency
	field = "deposit" if transaction.withdrawal > 0.0 else "withdrawal"
	filter_by_party = (
		"AND party_type = %(party_type)s AND party = %(party)s" if exact_party_match else ""
	)

	return f"""
		SELECT
			(
				CASE WHEN reference_number = %(reference_no)s THEN 1 ELSE 0 END
				+ CASE WHEN {field} = %(amount)s THEN 1 ELSE 0 END
				+ CASE WHEN ( party_type = %(party_type)s AND party = %(party)s ) THEN 1 ELSE 0 END
				+ CASE WHEN unallocated_amount = %(amount)s THEN 1 ELSE 0 END
				+ 1
			) AS rank,
			'Bank Transaction' AS doctype,
			name,
			unallocated_amount AS paid_amount,
			reference_number AS reference_no,
			date AS reference_date,
			party,
			party_type,
			date AS posting_date,
			currency,
			(
				CASE WHEN reference_number = %(reference_no)s THEN 1 ELSE 0 END
			) as reference_number_match,
			(
				CASE WHEN {field} = %(amount)s THEN 1 ELSE 0 END
			) as amount_match,
			(
				CASE WHEN ( party_type = %(party_type)s AND party = %(party)s ) THEN 1 ELSE 0 END
			) as party_match,
			(
				CASE WHEN unallocated_amount = %(amount)s THEN 1 ELSE 0 END
			) as unallocated_amount_match
		FROM
			`tabBank Transaction`
		WHERE
			status != 'Reconciled'
			AND name != '{transaction.name}'
			AND bank_account = '{transaction.bank_account}'
			AND {field} {'= %(amount)s' if exact_match else '> 0.0'}
			{filter_by_party}
	"""


def get_ld_matching_query(bank_account, exact_match, filters):
	loan_disbursement = frappe.qb.DocType("Loan Disbursement")
	matching_reference = loan_disbursement.reference_number == filters.get(
		"reference_number"
	)
	matching_party = loan_disbursement.applicant_type == filters.get(
		"party_type"
	) and loan_disbursement.applicant == filters.get("party")

	rank = frappe.qb.terms.Case().when(matching_reference, 1).else_(0)

	rank1 = frappe.qb.terms.Case().when(matching_party, 1).else_(0)

	query = (
		frappe.qb.from_(loan_disbursement)
		.select(
			rank + rank1 + 1,
			ConstantColumn("Loan Disbursement").as_("doctype"),
			loan_disbursement.name,
			loan_disbursement.disbursed_amount.as_("paid_amount"),
			loan_disbursement.reference_number.as_("reference_no"),
			loan_disbursement.reference_date,
			loan_disbursement.applicant.as_("party"),
			loan_disbursement.applicant_type.as_("party_type"),
			loan_disbursement.disbursement_date.as_("posting_date"),
			"".as_("currency"),
			rank.as_("reference_number_match"),
			rank1.as_("party_match"),
		)
		.where(loan_disbursement.docstatus == 1)
		.where(loan_disbursement.clearance_date.isnull())
		.where(loan_disbursement.disbursement_account == bank_account)
	)

	if exact_match:
		query.where(loan_disbursement.disbursed_amount == filters.get("amount"))
	else:
		query.where(loan_disbursement.disbursed_amount > 0.0)

	vouchers = query.run(as_list=True)

	return vouchers


def get_lr_matching_query(bank_account, exact_match, filters):
	loan_repayment = frappe.qb.DocType("Loan Repayment")
	matching_reference = loan_repayment.reference_number == filters.get("reference_number")
	matching_party = loan_repayment.applicant_type == filters.get(
		"party_type"
	) and loan_repayment.applicant == filters.get("party")

	rank = frappe.qb.terms.Case().when(matching_reference, 1).else_(0)

	rank1 = frappe.qb.terms.Case().when(matching_party, 1).else_(0)

	query = (
		frappe.qb.from_(loan_repayment)
		.select(
			rank + rank1 + 1,
			ConstantColumn("Loan Repayment").as_("doctype"),
			loan_repayment.name,
			loan_repayment.amount_paid.as_("paid_amount"),
			loan_repayment.reference_number.as_("reference_no"),
			loan_repayment.reference_date,
			loan_repayment.applicant.as_("party"),
			loan_repayment.applicant_type.as_("party_type"),
			loan_repayment.posting_date,
			"".as_("currency"),
			rank.as_("reference_number_match"),
			rank1.as_("party_match"),
		)
		.where(loan_repayment.docstatus == 1)
		.where(loan_repayment.clearance_date.isnull())
		.where(loan_repayment.payment_account == bank_account)
	)

	if frappe.db.has_column("Loan Repayment", "repay_from_salary"):
		query = query.where((loan_repayment.repay_from_salary == 0))

	if exact_match:
		query.where(loan_repayment.amount_paid == filters.get("amount"))
	else:
		query.where(loan_repayment.amount_paid > 0.0)

	vouchers = query.run()

	return vouchers


def get_pe_matching_query(
	exact_match,
	account_from_to,
	transaction,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
	exact_party_match,
):
	# get matching payment entries query
	to_from = "to" if transaction.deposit > 0.0 else "from"
	currency_field = f"paid_{to_from}_account_currency as currency"
	filter_by_date = f"AND posting_date between '{from_date}' and '{to_date}'"
	order_by = " posting_date"
	filter_by_reference_no = ""

	if cint(filter_by_reference_date):
		filter_by_date = (
			f"AND reference_date between '{from_reference_date}' and '{to_reference_date}'"
		)
		order_by = " reference_date"

	if frappe.flags.auto_reconcile_vouchers == True:
		filter_by_reference_no = f"AND reference_no = '{transaction.reference_number}'"

	filter_by_party = (
		"AND (party_type = %(party_type)s AND party = %(party)s )"
		if exact_party_match
		else ""
	)

	return f"""
		SELECT
			(CASE WHEN reference_no=%(reference_no)s THEN 1 ELSE 0 END
			+ CASE WHEN (party_type = %(party_type)s AND party = %(party)s ) THEN 1 ELSE 0 END
			+ CASE WHEN paid_amount = %(amount)s THEN 1 ELSE 0 END
			+ 1 ) AS rank,
			'Payment Entry' as doctype,
			name,
			paid_amount,
			reference_no,
			reference_date,
			party,
			party_type,
			posting_date,
			{currency_field},
			(CASE WHEN reference_no=%(reference_no)s THEN 1 ELSE 0 END) AS reference_number_match,
			(CASE WHEN (party_type = %(party_type)s AND party = %(party)s ) THEN 1 ELSE 0 END) AS party_match,
			(CASE WHEN paid_amount = %(amount)s THEN 1 ELSE 0 END) AS amount_match
		FROM
			`tabPayment Entry`
		WHERE
			docstatus = 1
			AND payment_type IN (%(payment_type)s, 'Internal Transfer')
			AND ifnull(clearance_date, '') = ""
			AND {account_from_to} = %(bank_account)s
			AND paid_amount {'= %(amount)s' if exact_match else '> 0.0'}
			{filter_by_date}
			{filter_by_reference_no}
			{filter_by_party}
		order by{order_by}
	"""


def get_je_matching_query(
	exact_match,
	transaction,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	# get matching journal entry query
	# We have mapping at the bank level
	# So one bank could have both types of bank accounts like asset and liability
	# So cr_or_dr should be judged only on basis of withdrawal and deposit and not account type
	cr_or_dr = "credit" if transaction.withdrawal > 0.0 else "debit"
	filter_by_date = f"AND je.posting_date between '{from_date}' and '{to_date}'"
	order_by = " je.posting_date"
	filter_by_reference_no = ""
	if cint(filter_by_reference_date):
		filter_by_date = (
			f"AND je.cheque_date between '{from_reference_date}' and '{to_reference_date}'"
		)
		order_by = " je.cheque_date"
	if frappe.flags.auto_reconcile_vouchers == True:
		filter_by_reference_no = f"AND je.cheque_no = '{transaction.reference_number}'"
	return f"""
		SELECT
			(CASE WHEN je.cheque_no=%(reference_no)s THEN 1 ELSE 0 END
			+ CASE WHEN jea.{cr_or_dr}_in_account_currency = %(amount)s THEN 1 ELSE 0 END
			+ 1) AS rank ,
			'Journal Entry' AS doctype,
			je.name,
			jea.{cr_or_dr}_in_account_currency AS paid_amount,
			je.cheque_no AS reference_no,
			je.cheque_date AS reference_date,
			je.pay_to_recd_from AS party,
			jea.party_type,
			je.posting_date,
			jea.account_currency AS currency,
			(CASE WHEN je.cheque_no=%(reference_no)s THEN 1 ELSE 0 END) AS reference_number_match,
			(CASE WHEN jea.{cr_or_dr}_in_account_currency = %(amount)s THEN 1 ELSE 0 END) AS amount_match
		FROM
			`tabJournal Entry Account` AS jea
		JOIN
			`tabJournal Entry` AS je
		ON
			jea.parent = je.name
		WHERE
			je.docstatus = 1
			AND je.voucher_type NOT IN ('Opening Entry')
			AND (je.clearance_date IS NULL OR je.clearance_date='0000-00-00')
			AND jea.account = %(bank_account)s
			AND jea.{cr_or_dr}_in_account_currency {'= %(amount)s' if exact_match else '> 0.0'}
			AND je.docstatus = 1
			{filter_by_date}
			{filter_by_reference_no}
			order by {order_by}
	"""


def get_si_matching_query(exact_match, exact_party_match):
	# get matching paid sales invoice query
	filter_by_party = " AND si.customer = %(party)s" if exact_party_match else ""

	return f"""
		SELECT
			( CASE WHEN si.customer = %(party)s  THEN 1 ELSE 0 END
			+ CASE WHEN sip.amount = %(amount)s THEN 1 ELSE 0 END
			+ 1 ) AS rank,
			'Sales Invoice' as doctype,
			si.name,
			sip.amount as paid_amount,
			si.name as reference_no,
			'' as reference_date,
			si.customer as party,
			'Customer' as party_type,
			si.posting_date,
			si.currency,
			(CASE WHEN si.customer=%(party)s THEN 1 ELSE 0 END) AS party_match,
			(CASE WHEN sip.amount = %(amount)s THEN 1 ELSE 0 END) AS amount_match
		FROM
			`tabSales Invoice Payment` as sip
		JOIN
			`tabSales Invoice` as si
		ON
			sip.parent = si.name
		WHERE
			si.docstatus = 1
			AND (sip.clearance_date is null or sip.clearance_date='0000-00-00')
			AND sip.account = %(bank_account)s
			AND sip.amount {'= %(amount)s' if exact_match else '> 0.0'}
			{filter_by_party}
	"""


def get_unpaid_si_matching_query(exact_match, exact_party_match):
	sales_invoice = frappe.qb.DocType("Sales Invoice")

	party_match = (
		frappe.qb.terms.Case()
		.when(sales_invoice.customer == Parameter("%(party)s"), 1)
		.else_(0)
	)
	amount_match = (
		frappe.qb.terms.Case()
		.when(sales_invoice.grand_total == Parameter("%(amount)s"), 1)
		.else_(0)
	)

	query = (
		frappe.qb.from_(sales_invoice)
		.select(
			(party_match + amount_match + 1).as_("rank"),
			PseudoColumn("'Sales Invoice' as doctype"),
			sales_invoice.name.as_("name"),
			sales_invoice.outstanding_amount.as_("paid_amount"),
			sales_invoice.name.as_("reference_no"),
			PseudoColumn("'' as reference_date"),
			sales_invoice.customer.as_("party"),
			PseudoColumn("'Customer' as party_type"),
			sales_invoice.posting_date,
			sales_invoice.currency,
			party_match.as_("party_match"),
			amount_match.as_("amount_match"),
		)
		.where(sales_invoice.docstatus == 1)
		.where(sales_invoice.is_return == 0)
		.where(sales_invoice.outstanding_amount > 0.0)
	)

	if exact_match:
		query = query.where(sales_invoice.grand_total == Parameter("%(amount)s"))

	if exact_party_match:
		query = query.where(sales_invoice.customer == Parameter("%(party)s"))

	return str(query)


def get_pi_matching_query(exact_match, exact_party_match):
	# get matching purchase invoice query when they are also used as payment entries (is_paid)
	filter_by_party = "AND supplier = %(party)s" if exact_party_match else ""

	return f"""
		SELECT
			( CASE WHEN supplier = %(party)s THEN 1 ELSE 0 END
			+ CASE WHEN paid_amount = %(amount)s THEN 1 ELSE 0 END
			+ 1 ) AS rank,
			'Purchase Invoice' as doctype,
			name,
			paid_amount,
			name as reference_no,
			'' as reference_date,
			supplier as party,
			'Supplier' as party_type,
			posting_date,
			currency,
			(CASE WHEN supplier=%(party)s THEN 1 ELSE 0 END) AS party_match,
			(CASE WHEN paid_amount = %(amount)s THEN 1 ELSE 0 END) AS amount_match
		FROM
			`tabPurchase Invoice`
		WHERE
			docstatus = 1
			AND is_paid = 1
			AND ifnull(clearance_date, '') = ""
			AND cash_bank_account = %(bank_account)s
			AND paid_amount {'= %(amount)s' if exact_match else '> 0.0'}
			{filter_by_party}
	"""


def get_unpaid_pi_matching_query(exact_match, exact_party_match):
	purchase_invoice = frappe.qb.DocType("Purchase Invoice")

	party_match = (
		frappe.qb.terms.Case()
		.when(purchase_invoice.supplier == Parameter("%(party)s"), 1)
		.else_(0)
	)
	amount_match = (
		frappe.qb.terms.Case()
		.when(purchase_invoice.grand_total == Parameter("%(amount)s"), 1)
		.else_(0)
	)

	query = (
		frappe.qb.from_(purchase_invoice)
		.select(
			(party_match + amount_match + 1).as_("rank"),
			PseudoColumn("'Purchase Invoice' as doctype"),
			purchase_invoice.name.as_("name"),
			purchase_invoice.outstanding_amount.as_("paid_amount"),
			purchase_invoice.name.as_("reference_no"),
			PseudoColumn("'' as reference_date"),
			purchase_invoice.supplier.as_("party"),
			PseudoColumn("'Supplier' as party_type"),
			purchase_invoice.posting_date,
			purchase_invoice.currency,
			party_match.as_("party_match"),
			amount_match.as_("amount_match"),
		)
		.where(purchase_invoice.docstatus == 1)
		.where(purchase_invoice.is_return == 0)
		.where(purchase_invoice.outstanding_amount > 0.0)
	)

	if exact_match:
		query = query.where(purchase_invoice.grand_total == Parameter("%(amount)s"))

	if exact_party_match:
		query = query.where(purchase_invoice.supplier == Parameter("%(party)s"))

	return str(query)
