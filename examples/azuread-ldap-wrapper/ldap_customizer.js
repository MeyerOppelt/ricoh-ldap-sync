'use strict';

// Customizer for AzureAD-LDAP-wrapper: adds each user's Entra "Company name"
// (Graph property companyName) to their LDAP entry as the "company" attribute,
// for ricoh-ldap-sync's LDAP_ATTR_COMPANY. Mount it at
// /app/customizer/ldap_customizer.js; the wrapper loads it at startup.

var customizer = {};

// Ask Graph for companyName as well. Only the $select list changes, so the
// wrapper's own filters (GRAPH_FILTER_USERS) stay as they are.
customizer.modifyGraphApiConfig = function (apiConfig, MS_GRAPH_SCOPE) {
    if (apiConfig.uri.indexOf('companyName') === -1) {
        apiConfig.uri = apiConfig.uri.replace('$select=', '$select=companyName,');
    }
    return apiConfig;
};

customizer.ModifyLDAPUser = function (ldapuser, azureuser) {
    if (typeof azureuser.companyName === 'string' && azureuser.companyName.trim() !== '') {
        ldapuser.company = azureuser.companyName.trim();
    }
    return ldapuser;
};

module.exports = customizer;
