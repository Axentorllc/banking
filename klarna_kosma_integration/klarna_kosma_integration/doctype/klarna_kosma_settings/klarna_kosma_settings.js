// Copyright (c) 2022, ALYF GmbH and contributors
// For license information, please see license.txt

frappe.ui.form.on('Klarna Kosma Settings', {
	refresh: (frm) => {
		if (frm.doc.enabled) {
			frm.add_custom_button(__('Sync Bank and Accounts'), () => {
				frm.trigger("refresh_banks");
			});

			frm.add_custom_button(__("Sync Transactions"), () => {
				frappe.prompt({
					fieldtype: "Link",
					options: "Bank Account",
					label: __("Bank Account"),
					fieldname: "bank_account",
					reqd: 1,
					get_query: () => {
						return {
							filters: {
								"kosma_account_id": ["is", "set"],
							}
						};
					},
				}, (data) => {
					new KlarnaKosmaConnect({
						frm: frm,
						account: data.bank_account
					});
				},
				__("Select a Bank Account"),
				__("Continue"));
			});
		}

	},

	refresh_banks: (frm) => {
		frappe.prompt({
			fieldtype: "Link",
			options: "Company",
			label: __("Company"),
			fieldname: "company",
			reqd: 1
		}, (data) => {
			new KlarnaKosmaConnect({
				frm: frm,
				company: data.company
			});
		},
		__("Select a company"),
		__("Continue"));
	}
});

class KlarnaKosmaConnect {
	constructor(opts) {
		Object.assign(this, opts);

		// Account is passed to fetch transactions and company to fetch accounts
		this.flow = this.account ? "transactions" : "accounts";
		this.init_kosma_connect();
	}

	async init_kosma_connect () {
		if (this.flow == "accounts") {
			// Renders XS2A App (which authenticates/gets consent)
			// and hands over control to server side for data fetch & business logic
			this.session = await this.get_client_token();
			this.render_xs2a_app();
		} else {
			// fetches data using the consent API without user intervention
			this.complete_transactions_flow();
		}
	}

	async get_client_token (){
		let session_data = await this.frm.call({
			method: "get_client_token",
			args: { current_flow: this.flow },
			freeze: true,
			freeze_message: __("Please wait. Redirecting to Bank...")
		}).then(resp => resp.message);
		return session_data;
	}

	async render_xs2a_app() {
		// Render XS2A with client token
		await this.load_script();
		window.onXS2AReady = this.startKlarnaOpenBankingXS2AApp();
	}

	load_script() {
		return new Promise(function (resolve, reject) {
			const src = "https://x.klarnacdn.net/xs2a/app-launcher/v0/xs2a-app-launcher.js";

			if (document.querySelector('script[src="' + src + '"]')) {
				resolve();
				return;
			}

			const el = document.createElement('script');
			el.type = 'text/javascript';
			el.async = true;
			el.src = src;
			el.addEventListener('load', resolve);
			el.addEventListener('error', reject);
			el.addEventListener('abort', reject);
			document.head.appendChild(el);
		});
	}

	startKlarnaOpenBankingXS2AApp() {
		let me = this;
		try {
			window.XS2A.startFlow(
				me.session.client_token,
				{
					unfoldConsentDetails: true,
					onFinished: () => {
						me.complete_accounts_flow();
					},
					onError: error => {
						console.error('onError: something bad happened.', error);
					},
					onAbort: () => {
						console.log("Kosma Authentication Aborted");
					},
				}
			)
		} catch (e) {
			console.error(e);
		}
	}

	async complete_accounts_flow() {
			let flow_data = await this.fetch_accounts_data();
			let bank_name = flow_data["message"]["bank_name"];

			this.add_bank_accounts(flow_data, bank_name);
	}

	async complete_transactions_flow()  {
		// Enqueue transactions fetch via Consent API
		await this.frm.call({
			method: "sync_transactions",
			args: { account: this.account },
			freeze: true,
			freeze_message: __("Please wait. Syncing Bank Transactions ...")
		});

	}

	async fetch_accounts_data() {
		try {
			const data = await this.frm.call({
				method: "fetch_accounts_and_bank",
				args: { session_id_short: this.session.session_id_short },
				freeze: true,
				freeze_message: __("Please wait. Fetching Bank Acounts ...")
			});

			if (!data.message || data.exc) {
				frappe.throw(__("Failed to fetch Bank Accounts."));
			}
			return data;
		} catch(e) {
			console.log(e);
		}
	}

	async add_bank_accounts(flow_data, bank_name) {
		let me = this;
		try {
			if (flow_data.message) {
				this.frm.call({
					method: "add_bank_accounts",
					args: {
						accounts: flow_data.message,
						company: me.company,
						bank_name: bank_name,
					},
					freeze: true,
					freeze_message: __("Adding Bank Acounts ...")
				}).then((r) => {
					if (!r.exc) {
						frappe.show_alert({ message: __("Bank accounts added"), indicator: 'green' });
					}
				});
			}
		} catch(e) {
			console.log(e);
		}
	}

}